/**
 * @file fit_writer.c
 * @brief FIT File Writer Implementation (LittleFS backend)
 * FIT Profile 21.138, compatible with Garmin Connect / Strava / Averyn
 */

#include <string.h>
#include <stdio.h>
#include <sys/stat.h>
#include "esp_log.h"
#include "esp_err.h"
#include "esp_timer.h"
#include "esp_spiffs.h"
#include "fit_writer.h"

static const char *TAG = "SYNAPSE_FIT";
static const char *BASE_PATH = "/storage";

static fit_session_t g_session = {0};
static bool g_initialized = false;
static FILE *g_file = NULL;
static uint32_t g_data_size = 0; // bytes after header (for header fixup)

// FIT CRC-16 (standard)
static uint16_t fit_crc16(const uint8_t *data, size_t len, uint16_t crc) {
    static const uint16_t crc_table[16] = {
        0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
        0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400
    };
    for (size_t i = 0; i < len; i++) {
        uint8_t byte = data[i];
        uint16_t tmp = crc_table[crc & 0xF];
        crc = (crc >> 4) & 0x0FFF;
        crc ^= tmp ^ crc_table[byte & 0xF];
        tmp = crc_table[crc & 0xF];
        crc = (crc >> 4) & 0x0FFF;
        crc ^= tmp ^ crc_table[(byte >> 4) & 0xF];
    }
    return crc;
}

static uint16_t running_crc = 0;
static void crc_write(const void *data, size_t len) {
    if (g_file && data && len) {
        fwrite(data, 1, len, g_file);
        running_crc = fit_crc16((const uint8_t*)data, len, running_crc);
        g_data_size += len;
    }
}

// Write FIT file header (14 bytes, placeholder data_size; fixed up on stop)
static esp_err_t write_header(void) {
    if (!g_file) return ESP_ERR_INVALID_STATE;
    uint8_t header[14];
    header[0] = 14; // header size
    header[1] = 0x10; // protocol version 1.0
    uint16_t profile_ver = 21138; // 21.138 little-endian-ish (actually 2113? use 0x2010)
    profile_ver = 0x0815;
    header[2] = profile_ver & 0xFF;
    header[3] = (profile_ver >> 8) & 0xFF;
    // data size placeholder (4 bytes LE)
    header[4] = 0; header[5] = 0; header[6] = 0; header[7] = 0;
    header[8] = '.'; header[9] = 'F'; header[10] = 'I'; header[11] = 'T';
    // header CRC placeholder
    header[12] = 0; header[13] = 0;
    if (fwrite(header, 1, 14, g_file) != 14) return ESP_FAIL;
    running_crc = 0;
    g_data_size = 0;
    return ESP_OK;
}

// Synapse developer field descriptors (flat scalars only, per FIT spec).
// Must match the Averyn consumer contract exactly.
typedef struct {
    uint8_t  field_num;
    uint8_t  size;
    uint8_t  base_type;
    const char *name;
} synapse_dev_field_t;

static const synapse_dev_field_t s_dev_fields[SYNAPSE_NUM_DEV_FIELDS] = {
    { SYNAPSE_DEV_FIELD_STRESS,       1, FIT_BASE_TYPE_ENUM,   "synapse_stress" },
    { SYNAPSE_DEV_FIELD_SQI,          1, FIT_BASE_TYPE_UINT8,   "synapse_sqi" },
    { SYNAPSE_DEV_FIELD_MAP,          1, FIT_BASE_TYPE_UINT8,   "synapse_map" },
    { SYNAPSE_DEV_FIELD_ECG_QUALITY,  1, FIT_BASE_TYPE_UINT8,   "synapse_ecg_quality" },
    { SYNAPSE_DEV_FIELD_SLEEP_STAGE,  1, FIT_BASE_TYPE_ENUM,    "synapse_sleep_stage" },
    { SYNAPSE_DEV_FIELD_RMSSD,        2, FIT_BASE_TYPE_UINT16,  "synapse_rmssd" },
    { SYNAPSE_DEV_FIELD_SDNN,         2, FIT_BASE_TYPE_UINT16,  "synapse_sdnn" },
    { SYNAPSE_DEV_FIELD_LF_HF,        2, FIT_BASE_TYPE_UINT16,  "synapse_lf_hf" },
};

// Write FIT Developer Data ID message (206): maps developer_data_index to an
// application version. One message per developer field index (0-7).
static void write_developer_data_id(void) {
    uint8_t def[] = {
        0x40, // definition, local msg type 1
        0x00,
        0x00,
        0xCE, 0x00, // global msg num = 206
        0x02, // num fields
        // field 0: developer_data_index (uint8)
        0, 1, FIT_BASE_TYPE_UINT8,
        // field 4: developer_application_version (uint16)
        4, 2, FIT_BASE_TYPE_UINT16,
    };
    crc_write(def, sizeof(def));

    // Data: one record per developer field index, all under application "SYNA"
    // (version 0x53594E41 stored as uint16 low word per FIT convention).
    for (uint8_t idx = 0; idx < SYNAPSE_NUM_DEV_FIELDS; idx++) {
        uint8_t data[] = {
            0x00, // data, local msg type 1
            idx,  // developer_data_index
            0x41, 0x53, // developer_application_version (little-endian low word)
        };
        crc_write(data, sizeof(data));
    }
}

// Write FIT Field Description message (207): name + base type of each dev field.
// Required so a standard FIT parser can decode the developer data block.
static void write_field_description(void) {
    uint8_t def[] = {
        0x40, // definition, local msg type 2
        0x00,
        0x00,
        0xCF, 0x00, // global msg num = 207
        0x04, // num fields
        // field 0: developer_data_index (uint8)
        0, 1, FIT_BASE_TYPE_UINT8,
        // field 1: fit_base_type_id (uint8)
        1, 1, FIT_BASE_TYPE_UINT8,
        // field 2: field_name (string)
        2, 17, FIT_BASE_TYPE_STRING,
        // field 3: field_definition_number (uint8)
        3, 1, FIT_BASE_TYPE_UINT8,
    };
    crc_write(def, sizeof(def));

    for (uint8_t i = 0; i < SYNAPSE_NUM_DEV_FIELDS; i++) {
        const synapse_dev_field_t *f = &s_dev_fields[i];
        // Field name: max 16 chars + null terminator = 17 bytes for STRING base type
        char name_buf[17] = {0};
        size_t n = strlen(f->name);
        if (n > 16) n = 16;
        memcpy(name_buf, f->name, n);

        uint8_t data[1 + 1 + 17 + 1];
        uint8_t *p = data;
        *p++ = 0x00; // data, local msg type 2
        *p++ = i;    // developer_data_index
        *p++ = f->base_type;
        memcpy(p, name_buf, 17);
        p += 17;
        *p++ = f->field_num; // field_definition_number
        crc_write(data, sizeof(data));
    }
}

// Write definition message for record (standard + Synapse developer data).
static void write_record_definition(void) {
    // Standard record definition: local msg type 0
    uint8_t def[] = {
        0x40, // header: definition, local msg type 0
        0x00, // reserved
        0x00, // arch little-endian
        0x14, 0x00, // global msg num = 20 (record)
        0x03, // num fields: timestamp, heart_rate, rr_interval
        // field defs: field_num, size, base_type
        253, 4, FIT_BASE_TYPE_UINT32,  // timestamp
        3, 1, FIT_BASE_TYPE_UINT8,     // heart_rate
        7, 2, FIT_BASE_TYPE_UINT16,    // rr_interval_ms (field 7)
    };
    crc_write(def, sizeof(def));

    // Developer data definition: local msg type 1
    uint8_t dev_def[] = {
        0x40, // header: definition, local msg type 1
        0x00,
        0x00,
        0xD1, 0x00, // global msg num = 209 (developer data)
        0x02, // num fields: timestamp + developer_data_index
        253, 4, FIT_BASE_TYPE_UINT32,  // timestamp
        0, 1, FIT_BASE_TYPE_UINT8,     // developer_data_index
    };
    crc_write(dev_def, sizeof(dev_def));

    // Register the developer data contract (messages 206/207) once per session.
    write_developer_data_id();
    write_field_description();
}

// Write a data record: standard fields + Synapse developer data block.
static esp_err_t write_record(const fit_record_t *record) {
    if (!g_file) return ESP_ERR_INVALID_STATE;

    // --- Standard record (local msg type 0) ---
    uint8_t hdr = 0x00; // data, local msg type 0
    crc_write(&hdr, 1);
    uint32_t ts = record->timestamp_utc;
    uint8_t ts_b[4] = { ts & 0xFF, (ts>>8)&0xFF, (ts>>16)&0xFF, (ts>>24)&0xFF };
    crc_write(ts_b, 4);
    uint8_t hr = (record->heart_rate > 255) ? 255 : (uint8_t)record->heart_rate;
    crc_write(&hr, 1);
    uint16_t rr = record->rr_interval_ms;
    uint8_t rr_b[2] = { rr & 0xFF, (rr>>8)&0xFF };
    crc_write(rr_b, 2);

    // --- Developer data (local msg type 1) ---
    uint8_t dev_hdr = 0x01; // data, local msg type 1
    crc_write(&dev_hdr, 1);
    crc_write(ts_b, 4); // timestamp
    crc_write(&(uint8_t){0}, 1); // developer_data_index 0

    // Write each developer field value in fixed order (0..7)
    uint8_t dev_vals[SYNAPSE_NUM_DEV_FIELDS * 2]; // max 2 bytes each
    uint8_t n = 0;
    for (int i = 0; i < SYNAPSE_NUM_DEV_FIELDS; i++) {
        const synapse_dev_field_t *f = &s_dev_fields[i];
        uint16_t u16_val = 0;
        uint8_t u8_val = 0;
        bool is_u16 = (f->size == 2);

        switch (f->field_num) {
        case SYNAPSE_DEV_FIELD_STRESS:       u8_val = record->stress_level; break;
        case SYNAPSE_DEV_FIELD_SQI:          u8_val = record->ppg_sqi; break;
        case SYNAPSE_DEV_FIELD_MAP:          u8_val = record->ppg_map; break;
        case SYNAPSE_DEV_FIELD_ECG_QUALITY:  u8_val = record->ecg_quality; break;
        case SYNAPSE_DEV_FIELD_SLEEP_STAGE:  u8_val = record->sleep_stage; break;
        case SYNAPSE_DEV_FIELD_RMSSD:        u16_val = record->rmssd; break;
        case SYNAPSE_DEV_FIELD_SDNN:         u16_val = record->sdnn; break;
        case SYNAPSE_DEV_FIELD_LF_HF:        u16_val = record->lf_hf; break;
        }

        if (is_u16) {
            dev_vals[n++] = u16_val & 0xFF;
            dev_vals[n++] = (u16_val >> 8) & 0xFF;
        } else {
            dev_vals[n++] = u8_val;
        }
    }
    crc_write(dev_vals, n);

    g_session.record_count++;
    if ((g_session.record_count % 10) == 0) fflush(g_file);
    return ESP_OK;
}

esp_err_t synapse_fit_init(void) {
    if (g_initialized) return ESP_OK;
    esp_vfs_spiffs_conf_t conf = {
        .base_path = BASE_PATH,
        .partition_label = "storage",
        .max_files = 8,
        .format_if_mount_failed = true,
    };
    esp_err_t ret = esp_vfs_spiffs_register(&conf);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "SPIFFS mount failed: %s", esp_err_to_name(ret));
        return ret;
    }
    size_t total = 0, used = 0;
    esp_spiffs_info(conf.partition_label, &total, &used);
    ESP_LOGI(TAG, "FIT storage mounted: total=%u used=%u", (unsigned)total, (unsigned)used);
    memset(&g_session, 0, sizeof(g_session));
    g_initialized = true;
    return ESP_OK;
}

esp_err_t synapse_fit_start_session(const fit_session_config_t *config) {
    if (!g_initialized) return ESP_ERR_INVALID_STATE;
    if (!config) return ESP_ERR_INVALID_ARG;
    if (g_session.active) return ESP_ERR_INVALID_STATE;

    memcpy(&g_session.config, config, sizeof(fit_session_config_t));
    snprintf(g_session.filepath, sizeof(g_session.filepath),
             "%s/session_%lu.fit", BASE_PATH, (unsigned long)config->start_time_utc);
    g_file = fopen(g_session.filepath, "wb");
    if (!g_file) {
        ESP_LOGE(TAG, "Failed to open %s", g_session.filepath);
        return ESP_FAIL;
    }
    ESP_ERROR_CHECK(write_header());
    write_record_definition();
    g_session.active = true;
    g_session.record_count = 0;
    g_session.session_start_ticks = (uint32_t)(esp_timer_get_time() / 1000);
    ESP_LOGI(TAG, "FIT session started: %s sport=%d", g_session.filepath, config->sport_type);
    return ESP_OK;
}

esp_err_t synapse_fit_write_record(const fit_record_t *record) {
    if (!g_initialized || !g_session.active || !g_file) return ESP_ERR_INVALID_STATE;
    if (!record) return ESP_ERR_INVALID_ARG;

    // Data message: header (local msg type 0) + fields
    uint8_t hdr = 0x00;
    crc_write(&hdr, 1);
    uint32_t ts = record->timestamp_utc;
    uint8_t ts_b[4] = { ts & 0xFF, (ts>>8)&0xFF, (ts>>16)&0xFF, (ts>>24)&0xFF };
    crc_write(ts_b, 4);
    uint8_t hr = (record->heart_rate > 255) ? 255 : (uint8_t)record->heart_rate;
    crc_write(&hr, 1);
    uint16_t spd = (uint16_t)(record->speed * 1000.0f);
    uint8_t spd_b[2] = { spd & 0xFF, (spd>>8)&0xFF };
    crc_write(spd_b, 2);
    crc_write(&record->stress_level, 1);
    crc_write(&record->ppg_sqi, 1);
    crc_write(&record->ppg_map, 1);

    g_session.record_count++;
    // Periodic flush for power-loss protection (every 10 records)
    if ((g_session.record_count % 10) == 0) fflush(g_file);
    return ESP_OK;
}

esp_err_t synapse_fit_write_from_feature(const synapse_feature_stream_t *feature,
                                          const double *gps_lat, const double *gps_lon,
                                          const double *gps_alt, const float *gps_speed,
                                          const int16_t *ecg_sample, const uint16_t *ppg_ir,
                                          const uint16_t *ppg_red, const int16_t accel[3],
                                          const int16_t gyro[3], const int16_t *temp) {
    if (!feature) return ESP_ERR_INVALID_ARG;
    if (!g_session.active) return ESP_ERR_INVALID_STATE;
    fit_record_t rec = {0};
    // Time base: session start (UTC, from provisioning/SNTP) + elapsed ticks.
    // Falls back to esp_timer approximation if session start was never set.
    uint32_t base = g_session.config.start_time_utc ? g_session.config.start_time_utc
                                                    : (uint32_t)(esp_timer_get_time() / 1000000);
    rec.timestamp_utc = base + (uint32_t)((esp_timer_get_time() / 1000) - g_session.session_start_ticks);
    rec.heart_rate = feature->heart_rate;
    rec.rr_interval_ms = feature->rr_interval_ms;
    rec.stress_level = feature->stress_level;
    rec.ppg_sqi = feature->ppg_sqi;
    rec.ppg_map = feature->ppg_map;
    rec.ecg_quality = feature->ecg_quality;
    rec.battery_level = feature->battery_level;
    rec.temperature = feature->temperature;
    rec.rmssd = feature->rmssd_ms;
    rec.sdnn = feature->sdnn_ms;
    if (gps_speed) rec.speed = *gps_speed;
    if (gps_lat) rec.lat = *gps_lat;
    if (gps_lon) rec.lon = *gps_lon;
    if (gps_alt) rec.altitude = *gps_alt;
    if (ecg_sample) rec.ecg_raw = *ecg_sample;
    if (ppg_ir) rec.ppg_raw_ir = *ppg_ir;
    if (ppg_red) rec.ppg_raw_red = *ppg_red;
    if (accel) { rec.accel_x = accel[0]; rec.accel_y = accel[1]; rec.accel_z = accel[2]; }
    if (gyro) { rec.gyro_x = gyro[0]; rec.gyro_y = gyro[1]; rec.gyro_z = gyro[2]; }
    if (temp) rec.temperature = *temp;
    return synapse_fit_write_record(&rec);
}

esp_err_t synapse_fit_stop_session(const char *output_path) {
    (void)output_path;
    if (!g_initialized || !g_session.active || !g_file) return ESP_ERR_INVALID_STATE;
    fflush(g_file);
    // Write file CRC
    uint8_t crc_b[2] = { running_crc & 0xFF, (running_crc >> 8) & 0xFF };
    fwrite(crc_b, 1, 2, g_file);
    long total = ftell(g_file);
    // Fix up header data_size = total - 14 - 2
    uint32_t data_size = (total > 16) ? (uint32_t)(total - 16) : 0;
    fseek(g_file, 4, SEEK_SET);
    uint8_t sz[4] = { data_size & 0xFF, (data_size>>8)&0xFF, (data_size>>16)&0xFF, (data_size>>24)&0xFF };
    fwrite(sz, 1, 4, g_file);
    // Header CRC over bytes 0..11
    fseek(g_file, 0, SEEK_SET);
    uint8_t hdr12[12];
    fread(hdr12, 1, 12, g_file);
    uint16_t hcrc = fit_crc16(hdr12, 12, 0);
    fseek(g_file, 12, SEEK_SET);
    uint8_t hcb[2] = { hcrc & 0xFF, (hcrc>>8)&0xFF };
    fwrite(hcb, 1, 2, g_file);
    fclose(g_file);
    g_file = NULL;
    ESP_LOGI(TAG, "FIT session stopped: %s records=%lu size=%ld",
             g_session.filepath, (unsigned long)g_session.record_count, total);
    g_session.active = false;
    return ESP_OK;
}

esp_err_t synapse_fit_get_status(bool *active, uint32_t *record_count) {
    if (active) *active = g_session.active;
    if (record_count) *record_count = g_session.record_count;
    return ESP_OK;
}

esp_err_t synapse_fit_list_files(char files[][64], int max_files, int *count) {
    // Minimal: not implemented for MVP (use LittleFS APIs in future)
    if (count) *count = 0;
    (void)files; (void)max_files;
    return ESP_OK;
}

esp_err_t synapse_fit_delete_file(const char *filename) {
    if (!filename) return ESP_ERR_INVALID_ARG;
    return remove(filename) == 0 ? ESP_OK : ESP_FAIL;
}

esp_err_t synapse_fit_validate_file(const char *filepath) {
    if (!filepath) return ESP_ERR_INVALID_ARG;
    struct stat st;
    if (stat(filepath, &st) != 0) return ESP_ERR_NOT_FOUND;
    if (st.st_size < 16) return ESP_FAIL;
    return ESP_OK;
}

esp_err_t synapse_fit_export_file(const char *src_path, const char *dst_path) {
    if (!src_path || !dst_path) return ESP_ERR_INVALID_ARG;
    FILE *src = fopen(src_path, "rb");
    if (!src) return ESP_ERR_NOT_FOUND;
    FILE *dst = fopen(dst_path, "wb");
    if (!dst) { fclose(src); return ESP_FAIL; }
    uint8_t buf[512];
    size_t n;
    while ((n = fread(buf, 1, sizeof(buf), src)) > 0) fwrite(buf, 1, n, dst);
    fclose(src); fclose(dst);
    return ESP_OK;
}
