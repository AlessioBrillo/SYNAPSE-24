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

// Write definition message for record (simplified: fixed layout)
static void write_record_definition(void) {
    // Definition: reserved(0), arch(0=little), global msg num (record=20), num fields
    // For MVP: timestamp(uint32), heart_rate(uint8), speed(float32), dev fields...
    // Simplified fixed definition
    uint8_t def[] = {
        0x40, // header: definition, local msg type 0
        0x00, // reserved
        0x00, // arch little-endian
        0x14, 0x00, // global msg num = 20 (record)
        0x06, // num fields = 6
        // field defs: field_num, size, base_type
        253, 4, FIT_BASE_TYPE_UINT32,  // timestamp
        3, 1, FIT_BASE_TYPE_UINT8,     // heart_rate
        6, 2, FIT_BASE_TYPE_UINT16,    // speed x1000? use uint16
        0, 1, FIT_BASE_TYPE_UINT8,     // dev stress
        1, 1, FIT_BASE_TYPE_UINT8,     // dev sqi
        2, 1, FIT_BASE_TYPE_UINT8,     // dev map
    };
    crc_write(def, sizeof(def));
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
    fit_record_t rec = {0};
    rec.timestamp_utc = (uint32_t)(esp_timer_get_time() / 1000000) + 631065600; // approx FIT epoch offset
    rec.heart_rate = feature->heart_rate;
    rec.rr_interval_ms = feature->rr_interval_ms;
    rec.stress_level = feature->stress_level;
    rec.ppg_sqi = feature->ppg_sqi;
    rec.ppg_map = feature->ppg_map;
    rec.ecg_quality = feature->ecg_quality;
    rec.battery_level = feature->battery_level;
    rec.temperature = feature->temperature;
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
