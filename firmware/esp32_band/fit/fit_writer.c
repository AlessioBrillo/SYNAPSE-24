/**
 * @file fit_writer.c
 * @brief FIT File Writer Implementation (LittleFS backend)
 * FIT Profile 21.138, compatible with Garmin Connect / Strava / Averyn
 */

#include <string.h>
#include <stdio.h>
#include <stdbool.h>
#include <sys/stat.h>
#include "sdkconfig.h"
#include "esp_log.h"
#include "esp_err.h"
#include "esp_timer.h"
#include "esp_spiffs.h"
#include "fit_writer.h"

static const char *TAG = "SYNAPSE_FIT";
// Mount point of the storage partition. Overridable so the host gate can
// compile the writer into a temporary directory (a SPIFFS mount point does
// not exist on the build host).
#ifndef SYNAPSE_FIT_BASE_PATH
#define SYNAPSE_FIT_BASE_PATH "/storage"
#endif
static const char *BASE_PATH = SYNAPSE_FIT_BASE_PATH;

static fit_session_t g_session = {0};
static bool g_initialized = false;
static FILE *g_file = NULL;
static uint32_t g_data_size = 0; // bytes after the header (for the header fixup)

// The file header is kept in memory: the data size and the header CRC are
// only known when the session stops, and a write-only file handle (SPIFFS)
// cannot be read back to recompute them.
static uint8_t g_header[FIT_FILE_HEADER_SIZE];
// CRC of the record data region (the bytes between the header and the
// trailing file CRC).
static uint16_t g_data_crc = 0;

// FIT CRC-16: the standard nibble-table CRC used by the FIT SDK and by every
// standard FIT parser (the same table the canonical Python writer pins).
static const uint16_t fit_crc_table[16] = {
    0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
    0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400
};

static uint16_t fit_crc16(const uint8_t *data, size_t len, uint16_t crc) {
    for (size_t i = 0; i < len; i++) {
        uint8_t byte = data[i];
        uint16_t tmp = fit_crc_table[crc & 0xF];
        crc = (crc >> 4) & 0x0FFF;
        crc ^= tmp ^ fit_crc_table[byte & 0xF];
        tmp = fit_crc_table[crc & 0xF];
        crc = (crc >> 4) & 0x0FFF;
        crc ^= tmp ^ fit_crc_table[(byte >> 4) & 0xF];
    }
    return crc;
}

/** One zero byte through the same update rule (see fit_crc16_file). */
static uint16_t fit_crc16_zero_byte(uint16_t crc) {
    uint16_t tmp = fit_crc_table[crc & 0xF];
    crc = (crc >> 4) & 0x0FFF;
    crc ^= tmp ^ fit_crc_table[0];
    tmp = fit_crc_table[crc & 0xF];
    crc = (crc >> 4) & 0x0FFF;
    crc ^= tmp ^ fit_crc_table[0];
    return crc;
}

/**
 * @brief CRC-16 of the whole file: the header followed by the record data.
 *
 * The last two bytes of a FIT file are a CRC-16 over every preceding byte
 * (the header included). The header is only final when the session stops
 * (the data size and the header CRC are patched then), so the value is
 * derived from the CRC of the record data with the header folded in.
 *
 * The CRC update is affine in its running state: for a fixed byte sequence
 * the final state is  A_len(seed) XOR CRC(seq, 0),  where A_len is the
 * linear map obtained by streaming len zero bytes. That identity is what
 * lets the header be folded in without re-reading the record data from
 * flash (a SPIFFS file opened for writing cannot be read back).
 *
 * @param header     The final 14-byte file header.
 * @param header_len Its length in bytes.
 * @param data_crc   CRC of the record data region (seed 0).
 * @param data_len   Length of the record data region in bytes.
 */
static uint16_t fit_crc16_file(const uint8_t *header, size_t header_len,
                               uint16_t data_crc, uint32_t data_len) {
    uint16_t propagated = fit_crc16(header, header_len, 0);
    for (uint32_t i = 0; i < data_len; i++) {
        propagated = fit_crc16_zero_byte(propagated);
    }
    return (uint16_t)(propagated ^ data_crc);
}

static void crc_write(const void *data, size_t len) {
    if (g_file && data && len) {
        fwrite(data, 1, len, g_file);
        g_data_crc = fit_crc16((const uint8_t*)data, len, g_data_crc);
        g_data_size += len;
    }
}

// Write FIT file header (14 bytes, placeholder data_size; fixed up on stop)
static esp_err_t write_header(void) {
    if (!g_file) return ESP_ERR_INVALID_STATE;
    memset(g_header, 0, sizeof(g_header));
    g_header[0] = FIT_FILE_HEADER_SIZE; // header size
    g_header[1] = FIT_PROTOCOL_VERSION; // protocol version 1.0
    g_header[2] = (uint8_t)(FIT_PROFILE_VERSION & 0xFF);
    g_header[3] = (uint8_t)((FIT_PROFILE_VERSION >> 8) & 0xFF);
    // data size placeholder (4 bytes LE), patched in synapse_fit_stop_session()
    g_header[8] = '.'; g_header[9] = 'F'; g_header[10] = 'I'; g_header[11] = 'T';
    // header CRC placeholder (2 bytes LE), patched in synapse_fit_stop_session()
    if (fwrite(g_header, 1, sizeof(g_header), g_file) != sizeof(g_header)) return ESP_FAIL;
    g_data_crc = 0;
    g_data_size = 0;
    return ESP_OK;
}

// Synapse developer field descriptors (flat scalars only, per FIT spec).
// Must match the Averyn consumer contract exactly:
// contracts/fit_developer_fields.json.
typedef struct {
    uint8_t  contract_id;  // contract id (0..7), immutable
    uint8_t  size;
    uint8_t  base_type;
    const char *name;
    const char *units;
} synapse_dev_field_t;

static const synapse_dev_field_t s_dev_fields[SYNAPSE_NUM_DEV_FIELDS] = {
    { SYNAPSE_DEV_FIELD_STRESS,       1, FIT_BASE_TYPE_ENUM,   "synapse_stress",      "enum" },
    { SYNAPSE_DEV_FIELD_SQI,          1, FIT_BASE_TYPE_UINT8,   "synapse_sqi",         "index" },
    { SYNAPSE_DEV_FIELD_MAP,          1, FIT_BASE_TYPE_UINT8,   "synapse_map",         "probability" },
    { SYNAPSE_DEV_FIELD_ECG_QUALITY,  1, FIT_BASE_TYPE_UINT8,   "synapse_ecg_quality", "confidence" },
    { SYNAPSE_DEV_FIELD_SLEEP_STAGE,  1, FIT_BASE_TYPE_ENUM,    "synapse_sleep_stage", "epoch label" },
    { SYNAPSE_DEV_FIELD_RMSSD,        2, FIT_BASE_TYPE_UINT16,  "synapse_rmssd",       "ms" },
    { SYNAPSE_DEV_FIELD_SDNN,         2, FIT_BASE_TYPE_UINT16,  "synapse_sdnn",        "ms" },
    { SYNAPSE_DEV_FIELD_LF_HF,        2, FIT_BASE_TYPE_UINT16,  "synapse_lf_hf",       "ratio_x100" },
};

// Write the FIT file_id message (global 0): the identity of the file. A
// standard parser expects it as the first message: without it the file
// carries no type, no manufacturer and no serial.
static void write_file_id(uint32_t serial_number, uint32_t time_created) {
    uint8_t def[] = {
        (uint8_t)(FIT_HEADER_DEFINITION | 2u), // definition, local msg type 2
        0x00, // reserved
        0x00, // architecture: little endian
        (uint8_t)(FIT_MESG_FILE_ID & 0xFF),
        (uint8_t)((FIT_MESG_FILE_ID >> 8) & 0xFF),
        0x05, // num fields
        // field 0: type (enum, 1 byte) — 4 = activity
        FIT_FIELD_FILE_ID_TYPE, 1, FIT_BASE_TYPE_ENUM,
        // field 1: manufacturer (uint16)
        FIT_FIELD_FILE_ID_MANUFACTURER, 2, FIT_BASE_TYPE_UINT16,
        // field 2: product (uint16)
        FIT_FIELD_FILE_ID_PRODUCT, 2, FIT_BASE_TYPE_UINT16,
        // field 3: serial_number (uint32z)
        FIT_FIELD_FILE_ID_SERIAL, 4, FIT_BASE_TYPE_UINT32Z,
        // field 4: time_created (uint32)
        FIT_FIELD_FILE_ID_TIME_CREATED, 4, FIT_BASE_TYPE_UINT32,
    };
    crc_write(def, sizeof(def));

    uint8_t data[1 + 1 + 2 + 2 + 4 + 4];
    uint8_t *p = data;
    *p++ = 0x02; // data message, local msg type 2
    *p++ = 0x04; // type: activity
    *p++ = (uint8_t)(FIT_MANUFACTURER_DEVELOPMENT & 0xFF);
    *p++ = (uint8_t)((FIT_MANUFACTURER_DEVELOPMENT >> 8) & 0xFF);
    *p++ = 0xFF; *p++ = 0xFF; // product: unknown
    uint32_t serial = serial_number;
    *p++ = (uint8_t)(serial & 0xFF);
    *p++ = (uint8_t)((serial >> 8) & 0xFF);
    *p++ = (uint8_t)((serial >> 16) & 0xFF);
    *p++ = (uint8_t)((serial >> 24) & 0xFF);
    *p++ = (uint8_t)(time_created & 0xFF);
    *p++ = (uint8_t)((time_created >> 8) & 0xFF);
    *p++ = (uint8_t)((time_created >> 16) & 0xFF);
    *p++ = (uint8_t)((time_created >> 24) & 0xFF);
    crc_write(data, (size_t)(p - data));
}

// Write FIT Developer Data ID message (global 207): binds a developer data
// index to the application that owns it. Field layout (FIT profile):
//   0 developer_id (byte[16]), 1 application_id (byte[16]),
//   2 manufacturer_id (uint16), 3 developer_data_index (uint8),
//   4 application_version (uint32)
static void write_developer_data_id(void) {
    uint8_t def[] = {
        (uint8_t)(FIT_HEADER_DEFINITION | 1u), // definition, local msg type 1
        0x00, // reserved
        0x00, // architecture: little endian
        (uint8_t)(FIT_MESG_DEVELOPER_DATA_ID & 0xFF),
        (uint8_t)((FIT_MESG_DEVELOPER_DATA_ID >> 8) & 0xFF),
        0x05, // num fields
        // field 0: developer_id (byte[16])
        FIT_FIELD_DEV_ID_DEVELOPER_ID, 16, FIT_BASE_TYPE_BYTE,
        // field 1: application_id (byte[16])
        FIT_FIELD_DEV_ID_APPLICATION_ID, 16, FIT_BASE_TYPE_BYTE,
        // field 2: manufacturer_id (uint16)
        FIT_FIELD_DEV_ID_MANUFACTURER, 2, FIT_BASE_TYPE_UINT16,
        // field 3: developer_data_index (uint8)
        FIT_FIELD_DEV_ID_INDEX, 1, FIT_BASE_TYPE_UINT8,
        // field 4: application_version (uint32)
        FIT_FIELD_DEV_ID_APPLICATION_VERSION, 4, FIT_BASE_TYPE_UINT32,
    };
    crc_write(def, sizeof(def));

    // One data message: all eight Synapse developer fields belong to the
    // same application and share the same developer data index.
    uint8_t data[1 + 16 + 16 + 2 + 1 + 4];
    uint8_t *p = data;
    *p++ = 0x01; // data message, local msg type 1
    memset(p, 0x00, 16); p += 16; // developer_id (unused)
    memcpy(p, FIT_APPLICATION_ID, sizeof(FIT_APPLICATION_ID) - 1); p += 16;
    *p++ = (uint8_t)(FIT_MANUFACTURER_DEVELOPMENT & 0xFF);
    *p++ = (uint8_t)((FIT_MANUFACTURER_DEVELOPMENT >> 8) & 0xFF);
    *p++ = FIT_DEVELOPER_DATA_INDEX;
    uint32_t ver = FIT_APPLICATION_VERSION;
    *p++ = (uint8_t)(ver & 0xFF);
    *p++ = (uint8_t)((ver >> 8) & 0xFF);
    *p++ = (uint8_t)((ver >> 16) & 0xFF);
    *p++ = (uint8_t)((ver >> 24) & 0xFF);
    crc_write(data, (size_t)(p - data));
}

// Write FIT Field Description message (global 206): names every developer
// field so a standard parser can decode the developer data block.
// Field layout (FIT profile):
//   0 developer_data_index (uint8), 1 field_definition_number (uint8),
//   2 fit_base_type_id (uint8), 3 field_name (string),
//   8 units (string)
static void write_field_description(void) {
    uint8_t def[] = {
        (uint8_t)(FIT_HEADER_DEFINITION | 2u), // definition, local msg type 2
        0x00, // reserved
        0x00, // architecture: little endian
        (uint8_t)(FIT_MESG_FIELD_DESCRIPTION & 0xFF),
        (uint8_t)((FIT_MESG_FIELD_DESCRIPTION >> 8) & 0xFF),
        0x05, // num fields
        // field 0: developer_data_index (uint8)
        FIT_FIELD_DESCRIPTION_INDEX, 1, FIT_BASE_TYPE_UINT8,
        // field 1: field_definition_number (uint8)
        FIT_FIELD_DESCRIPTION_NUMBER, 1, FIT_BASE_TYPE_UINT8,
        // field 2: fit_base_type_id (uint8)
        FIT_FIELD_DESCRIPTION_BASE_TYPE, 1, FIT_BASE_TYPE_UINT8,
        // field 3: field_name (string)
        FIT_FIELD_DESCRIPTION_NAME, FIT_FIELD_NAME_SIZE, FIT_BASE_TYPE_STRING,
        // field 8: units (string)
        FIT_FIELD_DESCRIPTION_UNITS, FIT_FIELD_UNITS_SIZE, FIT_BASE_TYPE_STRING,
    };
    crc_write(def, sizeof(def));

    for (uint8_t i = 0; i < SYNAPSE_NUM_DEV_FIELDS; i++) {
        const synapse_dev_field_t *f = &s_dev_fields[i];
        uint8_t data[1 + 1 + 1 + FIT_FIELD_NAME_SIZE + FIT_FIELD_UNITS_SIZE];
        uint8_t *p = data;
        *p++ = 0x02; // data message, local msg type 2
        *p++ = FIT_DEVELOPER_DATA_INDEX;
        *p++ = SYNAPSE_DEV_FIELD_DEFINITION_NUM(f->contract_id);
        *p++ = f->base_type;
        // FIT strings are fixed size and NUL padded.
        size_t n = strlen(f->name);
        if (n > FIT_FIELD_NAME_SIZE - 1) n = FIT_FIELD_NAME_SIZE - 1;
        memset(p, 0, FIT_FIELD_NAME_SIZE);
        memcpy(p, f->name, n);
        p += FIT_FIELD_NAME_SIZE;
        n = strlen(f->units);
        if (n > FIT_FIELD_UNITS_SIZE - 1) n = FIT_FIELD_UNITS_SIZE - 1;
        memset(p, 0, FIT_FIELD_UNITS_SIZE);
        memcpy(p, f->units, n);
        p += FIT_FIELD_UNITS_SIZE;
        crc_write(data, (size_t)(p - data));
    }
}

// Write definition message for the record (standard fields + the Synapse
// developer fields). The developer fields are declared in the record
// definition itself: that is what makes a standard parser able to read them.
static void write_record_definition(void) {
    // The developer data contract comes FIRST: a parser resolves the
    // developer field names and types from these messages, so they must be
    // known before the record definition that uses the developer fields.
    // The file_id (the identity of the file) comes before them: it is the
    // first message of every FIT file.
    write_file_id(FIT_INVALID_UINT32, g_session.config.start_time_utc);
    write_developer_data_id();
    write_field_description();

    // Standard record definition: local msg type 0, with the developer-data
    // flag set (the developer field definitions follow the standard ones).
    uint8_t def[] = {
        (uint8_t)(FIT_HEADER_DEFINITION | FIT_HEADER_DEVELOPER_DATA | 0u),
        0x00, // reserved
        0x00, // arch little-endian
        (uint8_t)(FIT_MESG_RECORD & 0xFF),
        (uint8_t)((FIT_MESG_RECORD >> 8) & 0xFF),
        0x02, // num fields: timestamp, heart_rate
        // field defs: field_num, size, base_type
        FIT_FIELD_RECORD_TIMESTAMP, 4, FIT_BASE_TYPE_UINT32,  // timestamp
        FIT_FIELD_RECORD_HEART_RATE, 1, FIT_BASE_TYPE_UINT8,  // heart_rate
        0x08, // num developer fields
    };
    crc_write(def, sizeof(def));

    // Developer field definitions: field definition number, size and the
    // developer data index (NOT a base type: the base type lives in the
    // field_description message).
    for (uint8_t i = 0; i < SYNAPSE_NUM_DEV_FIELDS; i++) {
        const synapse_dev_field_t *f = &s_dev_fields[i];
        uint8_t dev_def[3] = {
            SYNAPSE_DEV_FIELD_DEFINITION_NUM(f->contract_id),
            f->size,
            FIT_DEVELOPER_DATA_INDEX,
        };
        crc_write(dev_def, sizeof(dev_def));
    }
}

// Write a data record: standard fields + the Synapse developer data block.
// The developer values travel INSIDE the record message: the record
// definition declares them as developer fields, which is what the FIT
// profile prescribes and what a standard parser expects.
static esp_err_t synapse_fit_write_record(const fit_record_t *record) {
    if (!g_file) return ESP_ERR_INVALID_STATE;

    // --- Standard record (local msg type 0, developer-data flag set) ---
    uint8_t hdr = (uint8_t)(FIT_HEADER_DEVELOPER_DATA | 0u);
    crc_write(&hdr, 1);
    uint32_t ts = record->timestamp_utc;
    uint8_t ts_b[4] = { (uint8_t)(ts & 0xFF), (uint8_t)((ts>>8)&0xFF),
                        (uint8_t)((ts>>16)&0xFF), (uint8_t)((ts>>24)&0xFF) };
    crc_write(ts_b, 4);
    // The record heart_rate is a uint8: an invalid measurement is the uint8
    // invalid sentinel (0xFF), never a fabricated 0 bpm.
    uint8_t hr = (record->heart_rate == 0xFFFFu || record->heart_rate > 255u)
                     ? (uint8_t)FIT_INVALID_UINT8
                     : (uint8_t)record->heart_rate;
    crc_write(&hr, 1);

    // --- Developer data (declared in the record definition) ---
    uint8_t dev_vals[SYNAPSE_NUM_DEV_FIELDS * 2]; // max 2 bytes each
    uint8_t n = 0;
    for (int i = 0; i < SYNAPSE_NUM_DEV_FIELDS; i++) {
        const synapse_dev_field_t *f = &s_dev_fields[i];
        uint16_t u16_val = 0;
        uint8_t u8_val = 0;
        bool is_u16 = (f->size == 2);

        switch (f->contract_id) {
        case SYNAPSE_DEV_FIELD_STRESS:       u8_val = record->stress_level; break;
        case SYNAPSE_DEV_FIELD_SQI:          u8_val = record->ppg_sqi; break;
        case SYNAPSE_DEV_FIELD_MAP:          u8_val = record->ppg_map; break;
        case SYNAPSE_DEV_FIELD_ECG_QUALITY:  u8_val = record->ecg_quality; break;
        case SYNAPSE_DEV_FIELD_SLEEP_STAGE:  u8_val = record->sleep_stage; break;
        case SYNAPSE_DEV_FIELD_RMSSD:        u16_val = record->rmssd; break;
        case SYNAPSE_DEV_FIELD_SDNN:         u16_val = record->sdnn; break;
        case SYNAPSE_DEV_FIELD_LF_HF:        u16_val = record->lf_hf; break;
        default: break;
        }

        if (is_u16) {
            dev_vals[n++] = (uint8_t)(u16_val & 0xFF);
            dev_vals[n++] = (uint8_t)((u16_val >> 8) & 0xFF);
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
#if !defined(CONFIG_SYNAPSE_FEATURE_FIT_STORAGE)
    /* The FIT storage is compiled out (band-core build). The API stays
     * callable and succeeds: the acquisition FSM starts and stops a session
     * with ESP_ERROR_CHECK(), and aborting a device that simply has no FIT
     * flash would be worse than recording nothing. */
    ESP_LOGI(TAG, "FIT storage disabled by Kconfig: session recording is off");
    return ESP_OK;
#else
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
#endif
}

esp_err_t synapse_fit_start_session(const fit_session_config_t *config) {
#if !defined(CONFIG_SYNAPSE_FEATURE_FIT_STORAGE)
    /* No FIT storage in this build: the session is accepted and dropped (see
     * synapse_fit_init()). The acquisition FSM treats a failure here as
     * fatal, so the API must not fail. */
    (void)config;
    return ESP_OK;
#else
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
    esp_err_t hdr_rc = write_header();
    if (hdr_rc != ESP_OK) {
        ESP_LOGE(TAG, "FIT header write failed: %s", esp_err_to_name(hdr_rc));
        fclose(g_file);
        g_file = NULL;
        return hdr_rc;
    }
    write_record_definition();
    g_session.active = true;
    g_session.record_count = 0;
    g_session.session_start_ticks = (uint32_t)(esp_timer_get_time() / 1000);
    ESP_LOGI(TAG, "FIT session started: %s sport=%d", g_session.filepath, config->sport_type);
    return ESP_OK;
#endif
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
    // The feature stream carries no sleep stage: the contract's invalid
    // sentinel is written instead of a fabricated "wake" epoch.
    rec.sleep_stage = (uint8_t)FIT_INVALID_UINT8;
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
#if !defined(CONFIG_SYNAPSE_FEATURE_FIT_STORAGE)
    /* No FIT storage in this build: nothing to finalise (see
     * synapse_fit_init()). */
    return ESP_OK;
#else
    if (!g_initialized || !g_session.active || !g_file) return ESP_ERR_INVALID_STATE;
    fflush(g_file);

    // Patch the header: data size (bytes 4..7) and the header CRC (12..13).
    // The header is kept in memory because a SPIFFS file opened for writing
    // cannot be read back.
    g_header[4] = (uint8_t)(g_data_size & 0xFF);
    g_header[5] = (uint8_t)((g_data_size >> 8) & 0xFF);
    g_header[6] = (uint8_t)((g_data_size >> 16) & 0xFF);
    g_header[7] = (uint8_t)((g_data_size >> 24) & 0xFF);
    uint16_t hcrc = fit_crc16(g_header, 12, 0);
    g_header[12] = (uint8_t)(hcrc & 0xFF);
    g_header[13] = (uint8_t)((hcrc >> 8) & 0xFF);

    // File CRC: the last two bytes of the file are a CRC-16 over every
    // preceding byte (the header included). It is written once, at the end
    // of the file, and never per message.
    uint16_t file_crc = fit_crc16_file(g_header, sizeof(g_header),
                                        g_data_crc, g_data_size);
    uint8_t crc_b[2] = { (uint8_t)(file_crc & 0xFF),
                         (uint8_t)((file_crc >> 8) & 0xFF) };
    // The write position is still at the end of the record data: the CRC
    // goes there. The header is patched afterwards, from the start.
    bool failed = false;
    if (fwrite(crc_b, 1, FIT_CRC_SIZE, g_file) != FIT_CRC_SIZE) failed = true;
    if (!failed && fseek(g_file, 0, SEEK_SET) != 0) failed = true;
    if (!failed && fwrite(g_header, 1, sizeof(g_header), g_file) != sizeof(g_header)) {
        failed = true;
    }
    if (!failed) fflush(g_file);

    // The size is known: the header, the data and the trailing CRC. (It used
    // to be read back with ftell() *after* the seek to the start, so the log
    // always reported the header size.)
    long total = (long)FIT_FILE_HEADER_SIZE + (long)g_data_size + (long)FIT_CRC_SIZE;
    (void)total; // the log macros are no-ops in the host build

    fclose(g_file);
    g_file = NULL;
    g_session.active = false;
    if (failed) {
        // The file on the flash is truncated or has a broken header: it is
        // not a valid FIT file. The handle is released and the session is
        // closed: a failure here must not leave the pod with an open file
        // and an active session (the caller aborts the pod otherwise).
        ESP_LOGE(TAG, "FIT session could not be finalised: %s", g_session.filepath);
        return ESP_FAIL;
    }
    ESP_LOGI(TAG, "FIT session stopped: %s records=%lu size=%ld",
             g_session.filepath, (unsigned long)g_session.record_count, total);
    return ESP_OK;
#endif
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
