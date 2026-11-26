/**
 * @file ble_gatt_server.c
 * @brief Synapse Band v1 - BLE GATT Server (NimBLE, ESP-IDF 5.x)
 * Same stack as head/forearm/common. Standard: HR (0x180D), Battery (0x180F),
 * Device Info (0x180A). Custom 128-bit Synapse service (consumer contract v2): feature frame,
 * status, stream metadata and control point (the v1 feature characteristic is retired), plus
 * device config (R/W) and provisioning (W/N).
 */

#include <string.h>
#include <stdio.h>
#include <assert.h>
#include "sdkconfig.h"
#include "esp_log.h"
#include "esp_nimble_hci.h"
#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"
#include "host/ble_hs.h"
#include "host/ble_gap.h"
#include "host/util/util.h"
#include "services/gap/ble_svc_gap.h"
#include "services/gatt/ble_svc_gatt.h"
#include "ble_gatt_server.h"
#include "synapse_hr_measurement.h"
#include "ble_gatt_server_priv.h"
#include "ble_research_service.h"
#include "synapse_contract_generated.h"
#if defined(CONFIG_SYNAPSE_FEATURE_PROVISIONING)
#include "synapse_provisioning.h"
#endif
#include "synapse_config.h"
#include "synapse_calibration.h"
#include "synapse_identity.h"

static uint16_t g_conn_handle = BLE_HS_CONN_HANDLE_NONE;
static bool g_connected = false;
static bool g_prov_subscribed = false;
static uint16_t g_mtu = 23;

// Value handles are filled by NimBLE (val_handle) when the host starts the GATT
// server; they are 0 until then. ble_gatts_find_chr() returns an rc, not a handle.
static uint16_t s_prov_handle = 0;
static uint16_t s_hr_handle = 0;
static uint16_t s_batt_handle = 0;
static bool s_hr_subscribed = false;
static bool s_batt_subscribed = false;

static uint8_t s_body_location = 1; // wrist
static uint8_t s_batt_level = 100;
static bool s_batt_notify_pending = false;
static uint16_t s_last_hr_bpm = 0xFFFF;
static uint16_t s_last_rr_ms = 0xFFFF;
static int64_t s_last_hr_us = 0;  // when notify_hr() last saw a valid measurement
static char s_mfr_name[32] = "Synapse";
static char s_model_num[32] = "Band v1";
static char s_serial_num[32] = "SYN-000000";
static char s_fw_rev[32] = "1.0.0";
static char s_hw_rev[32] = "1.0";
static uint8_t s_cfg_buf[SYNAPSE_MAX_CONFIG_SIZE] = {0};
static uint16_t s_cfg_len = 0;

// 128-bit UUIDs (little-endian byte order, GATT v1 frozen) — generated from
// config/hardware.yaml, so the firmware cannot carry a second copy of the
// contract. Regenerate with: uv run python tools/generate_contracts.py
// Service: f9e08ffd-3d34-441f-97a6-1bf0958123f1
static const ble_uuid128_t SYN_SVC_UUID =
    SYNAPSE_CONTRACT_UUID_CONSUMER_SERVICE_INIT;
// Device config: bf68d647-0d02-479a-8d75-7964a51f332f
static const ble_uuid128_t CFG_UUID =
    SYNAPSE_CONTRACT_UUID_CONSUMER_DEVICE_CONFIG_INIT;
// Provisioning: 4e200cf1-94ac-4d63-a161-a03e5d3bb14a
static const ble_uuid128_t PROV_UUID =
    SYNAPSE_CONTRACT_UUID_CONSUMER_PROVISIONING_INIT;

// Consumer contract v2: NEW characteristics on the same service. v1 above is frozen
// (Averyn pins contracts/gatt_v1.json by sha256), so a v1 client never sees these.
static const ble_uuid128_t FEAT2_UUID = SYNAPSE_CONTRACT_UUID_CONSUMER_V2_FEATURE_INIT;
static const ble_uuid128_t STATUS_UUID = SYNAPSE_CONTRACT_UUID_CONSUMER_V2_STATUS_INIT;
static const ble_uuid128_t META_UUID = SYNAPSE_CONTRACT_UUID_CONSUMER_V2_STREAM_METADATA_INIT;
static const ble_uuid128_t CTL_UUID = SYNAPSE_CONTRACT_UUID_CONSUMER_V2_CONTROL_POINT_INIT;
static uint16_t s_feat2_handle = 0;
static uint16_t s_status_handle = 0;
static bool s_feat2_subscribed = false;
static bool s_status_subscribed = false;
static uint16_t s_feat2_seq = 0;
static uint8_t s_feat2_rate_hz = SYNAPSE_CONTRACT_V2_FEATURE_RATE_HZ;
static synapse_ble_status_provider_t s_status_provider = NULL;
static synapse_ble_transmit_handler_t s_transmit_handler = NULL;
static synapse_ble_time_handler_t s_time_handler = NULL;
// The 100 default in s_batt_level is a placeholder, not a measurement: v2 reports
// the level only once the power module has actually provided one.
static bool s_batt_known = false;
static bool s_mtu_warned = false;  // one warning per connection when the MTU cannot carry v2
static bool s_batt_charging = false;

// --- characteristic access callbacks -----------------------------------------

// --- consumer contract v2 ----------------------------------------------------------------

static int feat2_access(uint16_t conn, uint16_t attr, struct ble_gatt_access_ctxt *ctxt, void *arg) {
    (void)conn; (void)attr; (void)ctxt; (void)arg;
    return BLE_ATT_ERR_UNLIKELY;  // notify-only
}

static size_t build_status(uint8_t *buf, size_t cap) {
    synapse_status_v2_t st;
    memset(&st, 0, sizeof(st));
    if (s_status_provider != NULL) {
        s_status_provider(&st);
    }
    st.mtu = g_mtu;  // negotiated here, not by the application
    return synapse_status_v2_encode(&st, buf, cap);
}

static int status_access(uint16_t conn, uint16_t attr, struct ble_gatt_access_ctxt *ctxt, void *arg) {
    (void)conn; (void)attr; (void)arg;
    if (ctxt->op != BLE_GATT_ACCESS_OP_READ_CHR) return BLE_ATT_ERR_UNLIKELY;
    uint8_t buf[SYNAPSE_CONTRACT_V2_STATUS_BYTES];
    size_t n = build_status(buf, sizeof(buf));
    if (n == 0) return BLE_ATT_ERR_INSUFFICIENT_RES;
    return os_mbuf_append(ctxt->om, buf, (uint16_t)n) == 0 ? 0 : BLE_ATT_ERR_INSUFFICIENT_RES;
}

static int meta_access(uint16_t conn, uint16_t attr, struct ble_gatt_access_ctxt *ctxt, void *arg) {
    (void)conn; (void)attr; (void)arg;
    if (ctxt->op != BLE_GATT_ACCESS_OP_READ_CHR) return BLE_ATT_ERR_UNLIKELY;
    uint8_t buf[1 + SYNAPSE_CONTRACT_V2_STREAM_COUNT * SYNAPSE_CONTRACT_V2_META_ENTRY_BYTES];
    size_t n = synapse_stream_metadata_encode(buf, sizeof(buf));
    if (n == 0) return BLE_ATT_ERR_INSUFFICIENT_RES;
    return os_mbuf_append(ctxt->om, buf, (uint16_t)n) == 0 ? 0 : BLE_ATT_ERR_INSUFFICIENT_RES;
}

static void ctl_set_transmitting(bool on, void *ctx) {
    (void)ctx;
    if (s_transmit_handler != NULL) s_transmit_handler(on);
}

static void ctl_set_time(uint64_t epoch_ms, void *ctx) {
    (void)ctx;
    if (s_time_handler != NULL) s_time_handler(epoch_ms);  // >= 2020, checked by the parser
}

static void ctl_set_feature_rate(uint8_t hz, void *ctx) {
    (void)ctx;
    s_feat2_rate_hz = hz;  // validated 1..SYNAPSE_CONTRACT_V2_FEATURE_RATE_MAX_HZ by the parser
}

static int ctl_access(uint16_t conn, uint16_t attr, struct ble_gatt_access_ctxt *ctxt, void *arg) {
    (void)conn; (void)attr; (void)arg;
    if (ctxt->op != BLE_GATT_ACCESS_OP_WRITE_CHR) return BLE_ATT_ERR_UNLIKELY;
    uint8_t req[16];  // the longest write is set_time: 1 + 8 bytes
    uint16_t len = 0;
    if (ble_hs_mbuf_to_flat(ctxt->om, req, sizeof(req), &len) != 0) {
        return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;
    }
    synapse_control_ops_t ops = {
        .set_transmitting = ctl_set_transmitting,
        .set_feature_rate = ctl_set_feature_rate,
        .set_time = ctl_set_time,
        .ctx = NULL,
    };
    int err = synapse_control_apply(req, len, &ops);
    // ATT application errors live in 0x80..0x9F: the contract error code rides in the low bits.
    return err == 0 ? 0 : (0x80 + err);
}

// Heart Rate Measurement (0x2A37): readable (last value) + notify.
//
// BLE Heart Rate Service (0x180D) / Heart Rate Measurement (0x2A37) flag
// semantics (Bluetooth SIG, Heart Rate Service):
//   bit0 (0x01) -> the Heart Rate Measurement Value is a 16-bit uint16
//                  (little-endian) instead of an 8-bit uint8;
//   bit4 (0x10) -> the standard "RR-Interval present" bit: one or more RR
//                  uint16 fields follow the HR value only when it is set.
//                  The unit is 1/1024 s (NOT milliseconds): a spec-correct
//                  client multiplies by 1000/1024, so a value sent in ms
//                  would read 2.4 % too short.
//
// The read path and the notify path MUST agree: a flag that announces a
// field the payload does not carry makes every client mis-parse the value
// (and an uninitialised byte reaches the client), so the flags are computed
// from the data that is actually sent.
// The device computes RR in milliseconds; the characteristic carries 1/1024 s.
static uint16_t rr_ms_to_1024(uint16_t rr_ms) {
    uint32_t v = ((uint32_t)rr_ms * 1024u + 500u) / 1000u;
    return (uint16_t)(v > 0xFFFEu ? 0xFFFEu : v);  // 0xFFFF stays the "no RR" sentinel upstream
}

static int hr_access(uint16_t conn, uint16_t attr, struct ble_gatt_access_ctxt *ctxt, void *arg) {
    (void)conn; (void)attr; (void)arg;
    if (ctxt->op == BLE_GATT_ACCESS_OP_READ_CHR) {
        // 0x2A37 has no "not available" code: 65535 bpm (the initial value) or a value the
        // finger left behind would read as a measurement. No fresh measurement, no read.
        if (s_last_hr_bpm == 0xFFFF ||
            !synapse_hr_is_readable(esp_timer_get_time(), s_last_hr_us)) {
            return BLE_ATT_ERR_UNLIKELY;
        }
        uint8_t val[1 + 2 + 2]; // flags + HR16 (+ RR16 when present)
        uint16_t rr = s_last_rr_ms;
        uint8_t flags = synapse_hr_flags(rr != 0xFFFF);
        val[0] = flags;
        val[1] = (uint8_t)(s_last_hr_bpm & 0xFF);
        val[2] = (uint8_t)((s_last_hr_bpm >> 8) & 0xFF);
        uint8_t n = 3;
        if (rr != 0xFFFF) {
            uint16_t rr_1024 = rr_ms_to_1024(rr);
            val[3] = (uint8_t)(rr_1024 & 0xFF);
            val[4] = (uint8_t)((rr_1024 >> 8) & 0xFF);
            n = 5;
        }
        int rc = os_mbuf_append(ctxt->om, val, n);
        return rc == 0 ? 0 : BLE_ATT_ERR_INSUFFICIENT_RES;
    }
    return BLE_ATT_ERR_UNLIKELY;
}

/**
 * Build the read-only device-config snapshot.
 *
 * The host validator reads this before the recording starts and compares it
 * with config/hardware.yaml. It is the proof that the flashed unit really
 * runs the rates, gain, LED currents and battery capacity the manifest says —
 * a stale sdkconfig used to produce a plausible but wrongly scaled dataset
 * (the gain trap in docs/hardware/FIRST_HARDWARE_DAY.md).
 */
/** Keep an operator-entered string from breaking the JSON readback. */
static void json_safe(char *dst, size_t cap, const char *src) {
    size_t j = 0;
    if (src == NULL || cap == 0) {
        if (cap) dst[0] = '\0';
        return;
    }
    for (; *src != '\0' && j + 1 < cap; src++) {
        char c = *src;
        dst[j++] = (c == '"' || c == '\\') ? ' ' : c;
    }
    dst[j] = '\0';
}

static void build_config_readback(uint8_t *buf, size_t cap) {
    synapse_hw_config_t cfg;
    memset(&cfg, 0, sizeof(cfg));
    if (synapse_config_get(&cfg) != ESP_OK) {
        snprintf((char *)buf, cap, "{\"v\":1,\"error\":\"config_unavailable\"}");
        return;
    }
    uint32_t gain_x100 = (uint32_t)CONFIG_SYNAPSE_ECG_GAIN;
    uint8_t led_red = cfg.ppg_led_current_red;
    uint8_t led_ir = cfg.ppg_led_current_ir;
    synapse_calibration_t cal;
    char cal_serial[SYNAPSE_CAL_SERIAL_MAX] = {0};
    char cal_date[SYNAPSE_CAL_DATE_MAX] = {0};
    char cal_operator[SYNAPSE_CAL_OPERATOR_MAX] = {0};
    const char *cal_state = "none";
    if (synapse_calibration_load(&cal) == ESP_OK && synapse_calibration_valid(&cal)) {
        if (cal.ecg_gain_x100 != 0) gain_x100 = cal.ecg_gain_x100;
        if (cal.ppg_led_red != 0) led_red = cal.ppg_led_red;
        if (cal.ppg_led_ir != 0) led_ir = cal.ppg_led_ir;
        cal_state = synapse_calibration_state();
        json_safe(cal_serial, sizeof(cal_serial), cal.serial);
        json_safe(cal_date, sizeof(cal_date), cal.date);
        json_safe(cal_operator, sizeof(cal_operator), cal.operator);
    }

    /* Every active hardware.yaml field the host gate checks is reported
     * here, with short keys so a long BLE read stays compact. A stale
     * sdkconfig can no longer change an unchecked sensor setting. */
    snprintf((char *)buf, cap,
             "{\"v\":1,\"fw\":\"%s\",\"ecg_hz\":%u,\"ppg_hz\":%u,\"imu_hz\":%u,"
             "\"ecg_gain_x100\":%lu,\"ppg_led_red\":%u,\"ppg_led_ir\":%u,"
             "\"battery_mah\":%u,\"ecg_adc_ch\":%d,\"ecg_lead_off\":%d,"
             "\"ppg_addr\":%u,\"ppg_sr\":%u,\"ppg_pw\":%u,\"ppg_adc\":%u,"
             "\"imu_addr\":%u,\"accel_g\":%d,\"gyro_dps\":%d,"
             "\"accel_hz\":%u,\"gyro_hz\":%u,"
             "\"i2c_port\":%d,\"i2c_sda\":%d,\"i2c_scl\":%d,\"i2c_hz\":%lu,"
             "\"sync_gpio\":%u,\"ina_addr\":%u,\"ina_shunt\":%u,"
             "\"power_profile\":%u,"
             "\"cal_serial\":\"%s\",\"cal_state\":\"%s\","
             "\"cal_date\":\"%s\",\"cal_operator\":\"%s\"}",
             synapse_config_get_fw_version(),
             (unsigned)SYNAPSE_CONTRACT_RATE_ECG_HZ,
             (unsigned)synapse_ppg_rate_hz(cfg.ppg_sample_rate),
             (unsigned)cfg.imu_accel_odr_hz,
             (unsigned long)gain_x100, (unsigned)led_red, (unsigned)led_ir,
             (unsigned)cfg.hub_battery_mah,
             (int)cfg.ecg_adc_channel, (int)cfg.ecg_gpio_lead_off,
             (unsigned)cfg.ppg_i2c_addr, (unsigned)cfg.ppg_sample_rate,
             (unsigned)cfg.ppg_pulse_width, (unsigned)cfg.ppg_adc_range,
             (unsigned)cfg.imu_i2c_addr, (int)cfg.imu_accel_fsr_g,
             (int)cfg.imu_gyro_fsr_dps, (unsigned)cfg.imu_accel_odr_hz,
             (unsigned)cfg.imu_gyro_odr_hz,
             (int)cfg.ppg_i2c_port, (int)cfg.i2c_sda_gpio, (int)cfg.i2c_scl_gpio,
             (unsigned long)cfg.i2c_freq_hz,
             (unsigned)SYNAPSE_CONTRACT_WIRED_SYNC_GPIO,
             (unsigned)SYNAPSE_CONTRACT_INA219_I2C_ADDR,
             (unsigned)SYNAPSE_CONTRACT_INA219_SHUNT_MOHM,
             (unsigned)cfg.power_profile,
             cal_serial, cal_state, cal_date, cal_operator);
}

static int cfg_access(uint16_t conn, uint16_t attr, struct ble_gatt_access_ctxt *ctxt, void *arg) {
    (void)conn; (void)attr; (void)arg;
    if (ctxt->op == BLE_GATT_ACCESS_OP_READ_CHR) {
        build_config_readback(s_cfg_buf, sizeof(s_cfg_buf));
        s_cfg_len = (uint16_t)strlen((const char *)s_cfg_buf);
        int rc = os_mbuf_append(ctxt->om, s_cfg_buf, s_cfg_len);
        return rc == 0 ? 0 : BLE_ATT_ERR_INSUFFICIENT_RES;
    }
    if (ctxt->op == BLE_GATT_ACCESS_OP_WRITE_CHR) {
#if !defined(CONFIG_SYNAPSE_FACTORY_MODE)
        ESP_LOGW(BLE_TAG, "config write refused: factory mode off");
        return BLE_ATT_ERR_WRITE_NOT_PERMITTED;
#else
        /* Contract 2.4, deliberately narrow: only the per-unit values the
         * calibration record already owns, plus the consumer feature rate.
         * Rates, pins and UUIDs stay compile-time contract values. */
        uint8_t req[96];
        uint16_t len = 0;
        if (ble_hs_mbuf_to_flat(ctxt->om, req, sizeof(req), &len) != 0) {
            return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;
        }
        req[len < sizeof(req) ? len : sizeof(req) - 1] = '\0';
        unsigned red = 0, ir = 0, gain = 0, rate = 0;
        if (sscanf((const char *)req, "{\"led_red\":%u,\"led_ir\":%u}", &red, &ir) == 2 &&
            red <= 255 && ir <= 255) {
            synapse_calibration_t cal;
            esp_err_t err = synapse_calibration_load(&cal);
            if (err != ESP_OK) {
                synapse_calibration_defaults(&cal);
                const char *serial = synapse_identity_serial();
                if (serial != NULL) snprintf(cal.serial, sizeof(cal.serial), "%s", serial);
            }
            if (red != 0) cal.ppg_led_red = (uint8_t)red;
            if (ir != 0) cal.ppg_led_ir = (uint8_t)ir;
            synapse_calibration_seal(&cal);
            if (synapse_calibration_save(&cal) != ESP_OK) return BLE_ATT_ERR_UNLIKELY;
            ESP_LOGI(BLE_TAG, "config write: LED red=%u ir=%u", red, ir);
            return 0;
        }
        if (sscanf((const char *)req, "{\"gain_x100\":%u}", &gain) == 1 && gain > 0) {
            synapse_calibration_t cal;
            esp_err_t err = synapse_calibration_load(&cal);
            if (err != ESP_OK) {
                synapse_calibration_defaults(&cal);
                const char *serial = synapse_identity_serial();
                if (serial != NULL) snprintf(cal.serial, sizeof(cal.serial), "%s", serial);
            }
            cal.ecg_gain_x100 = gain;
            synapse_calibration_seal(&cal);
            if (synapse_calibration_save(&cal) != ESP_OK) return BLE_ATT_ERR_UNLIKELY;
            ESP_LOGI(BLE_TAG, "config write: ECG gain=%ux100", gain);
            return 0;
        }
        if (sscanf((const char *)req, "{\"feature_rate_hz\":%u}", &rate) == 1 &&
            rate >= 1 && rate <= SYNAPSE_CONTRACT_V2_FEATURE_RATE_MAX_HZ) {
            ctl_set_feature_rate((uint8_t)rate, NULL);
            ESP_LOGI(BLE_TAG, "config write: feature rate=%u Hz", rate);
            return 0;
        }
        ESP_LOGW(BLE_TAG, "config write refused: unknown or out-of-range payload");
        return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;
#endif
    }
    return BLE_ATT_ERR_UNLIKELY;
}

/**
 * @brief Notify Heart Rate Measurement with optional RR interval.
 * Builds a standard 0x2A37 value: flags(0x01 = 16-bit HR, 0x10 = RR present)
 * + HR16 + [RR16...].
 * Call whenever a new HR sample is available; subscribers get the push.
 */
esp_err_t synapse_ble_gatt_notify_hr(uint16_t hr_bpm, uint16_t rr_interval_ms) {
    if (!g_connected || g_conn_handle == BLE_HS_CONN_HANDLE_NONE) return ESP_ERR_INVALID_STATE;
    // Keep the last value so a client that READS 0x2A37 (instead of waiting
    // for a notification) sees the same measurement the notify path sends.
    s_last_hr_bpm = hr_bpm;
    s_last_rr_ms = rr_interval_ms;
    s_last_hr_us = esp_timer_get_time();
    if (s_hr_handle == 0 || !s_hr_subscribed) return ESP_ERR_INVALID_STATE;
    // Standard 0x2A37 flags: 16-bit HR, sensor contact supported + detected, RR if present.
    const uint8_t flags = synapse_hr_flags(rr_interval_ms != 0xFFFF);
    uint8_t val[1 + 2 + 2]; // flags + HR16 [+ RR16]
    uint8_t n = 0;
    val[n++] = flags;
    val[n++] = (uint8_t)(hr_bpm & 0xFF);
    val[n++] = (uint8_t)((hr_bpm >> 8) & 0xFF);
    if (rr_interval_ms != 0xFFFF) {
        uint16_t rr_1024 = rr_ms_to_1024(rr_interval_ms);
        val[n++] = (uint8_t)(rr_1024 & 0xFF);
        val[n++] = (uint8_t)((rr_1024 >> 8) & 0xFF);
    }
    struct os_mbuf *om = ble_hs_mbuf_from_flat(val, n);
    if (!om) return ESP_ERR_NO_MEM;
    int rc = ble_gatts_notify_custom(g_conn_handle, s_hr_handle, om);
    return rc == 0 ? ESP_OK : ESP_FAIL;
}

static int body_loc_access(uint16_t conn, uint16_t attr, struct ble_gatt_access_ctxt *ctxt, void *arg) {
    (void)conn; (void)attr; (void)arg;
    if (ctxt->op == BLE_GATT_ACCESS_OP_READ_CHR) {
        os_mbuf_append(ctxt->om, &s_body_location, sizeof(s_body_location));
        return 0;
    }
    return BLE_ATT_ERR_UNLIKELY;
}

static int batt_access(uint16_t conn, uint16_t attr, struct ble_gatt_access_ctxt *ctxt, void *arg) {
    (void)conn; (void)attr; (void)arg;
    if (ctxt->op == BLE_GATT_ACCESS_OP_READ_CHR) {
        // Before the power module has measured the battery, 100 % would be a made-up value:
        // refuse the read instead (a client retries or shows nothing).
        if (!s_batt_known) return BLE_ATT_ERR_UNLIKELY;
        os_mbuf_append(ctxt->om, &s_batt_level, sizeof(s_batt_level));
        return 0;
    }
    return BLE_ATT_ERR_UNLIKELY;
}

/**
 * @brief Notify Battery Level (0x2A19) if subscribers exist.
 */
esp_err_t synapse_ble_gatt_notify_battery(uint8_t level, bool charging) {
    (void)charging;
    s_batt_level = level;
    s_batt_known = true;  // a notified level is a measured one
    s_batt_notify_pending = true;
    if (!g_connected || g_conn_handle == BLE_HS_CONN_HANDLE_NONE) return ESP_ERR_INVALID_STATE;
    if (s_batt_handle == 0 || !s_batt_subscribed) return ESP_ERR_INVALID_STATE;
    struct os_mbuf *om = ble_hs_mbuf_from_flat(&s_batt_level, 1);
    if (!om) return ESP_ERR_NO_MEM;
    int rc = ble_gatts_notify_custom(g_conn_handle, s_batt_handle, om);
    return rc == 0 ? ESP_OK : ESP_FAIL;
}

static int prov_access(uint16_t conn, uint16_t attr, struct ble_gatt_access_ctxt *ctxt, void *arg) {
    (void)conn; (void)attr; (void)arg;
    if (ctxt->op == BLE_GATT_ACCESS_OP_WRITE_CHR) {
        // [cmd u8][reserved u8][data_len u16 LE][data...]. The length in the header is the
        // client's claim: it is checked against the bytes the packet really carries, or the
        // handler would read (and act on) whatever is left on the stack past the end.
        const uint16_t len = OS_MBUF_PKTLEN(ctxt->om);
        if (len < 4) return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;
        uint8_t hdr[4];
        if (os_mbuf_copydata(ctxt->om, 0, 4, hdr) != 0) return BLE_ATT_ERR_UNLIKELY;
        const uint16_t data_len = hdr[2] | ((uint16_t)hdr[3] << 8);
        uint8_t payload[240] = {0};
        if (data_len > sizeof(payload) || (uint32_t)data_len + 4u != len) {
            return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;
        }
        if (data_len && os_mbuf_copydata(ctxt->om, 4, data_len, payload) != 0) {
            return BLE_ATT_ERR_UNLIKELY;
        }
#if defined(CONFIG_SYNAPSE_FEATURE_PROVISIONING)
        const esp_err_t pr = synapse_provisioning_process_cmd(hdr[0], payload, data_len);
        if (pr != ESP_OK) {
            ESP_LOGW(BLE_TAG, "provisioning command 0x%02x failed: %s", hdr[0], esp_err_to_name(pr));
            return BLE_ATT_ERR_UNLIKELY;  // the client must know it did not work
        }
#else
        // band-core: no provisioning. The write is refused, not silently
        // swallowed: a client that tries to provision must know it failed.
        (void)payload;
        ESP_LOGW(BLE_TAG, "provisioning is disabled in this build");
        return BLE_ATT_ERR_UNLIKELY;
#endif
        return 0;
    }
    return BLE_ATT_ERR_UNLIKELY;
}

static int dis_str_access(uint16_t conn, uint16_t attr, struct ble_gatt_access_ctxt *ctxt, void *arg) {
    (void)conn; (void)attr;
    const char *str = (const char *)arg;
    if (ctxt->op == BLE_GATT_ACCESS_OP_READ_CHR) {
        os_mbuf_append(ctxt->om, str, strlen(str));
        return 0;
    }
    return BLE_ATT_ERR_UNLIKELY;
}

// --- GATT database ------------------------------------------------------------

static const struct ble_gatt_svc_def gatt_svr_svcs[] = {
    {
        .type = BLE_GATT_SVC_TYPE_PRIMARY,
        .uuid = BLE_UUID16_DECLARE(0x180D),
        .characteristics = (struct ble_gatt_chr_def[]){
            {
                .uuid = BLE_UUID16_DECLARE(0x2A37),
                .access_cb = hr_access,
                .val_handle = &s_hr_handle,
                .flags = BLE_GATT_CHR_F_READ | BLE_GATT_CHR_F_NOTIFY,
            },
            {
                .uuid = BLE_UUID16_DECLARE(0x2A38),
                .access_cb = body_loc_access,
                .flags = BLE_GATT_CHR_F_READ,
            },
            {0},
        },
    },
    {
        .type = BLE_GATT_SVC_TYPE_PRIMARY,
        .uuid = BLE_UUID16_DECLARE(0x180F),
        .characteristics = (struct ble_gatt_chr_def[]){
            {
                .uuid = BLE_UUID16_DECLARE(0x2A19),
                .access_cb = batt_access,
                .val_handle = &s_batt_handle,
                .flags = BLE_GATT_CHR_F_READ | BLE_GATT_CHR_F_NOTIFY,
            },
            {0},
        },
    },
    {
        .type = BLE_GATT_SVC_TYPE_PRIMARY,
        .uuid = BLE_UUID16_DECLARE(0x180A),
        .characteristics = (struct ble_gatt_chr_def[]){
            {
                .uuid = BLE_UUID16_DECLARE(0x2A29),
                .access_cb = dis_str_access,
                .arg = (void *)s_mfr_name,
                .flags = BLE_GATT_CHR_F_READ,
            },
            {
                .uuid = BLE_UUID16_DECLARE(0x2A24),
                .access_cb = dis_str_access,
                .arg = (void *)s_model_num,
                .flags = BLE_GATT_CHR_F_READ,
            },
            {
                .uuid = BLE_UUID16_DECLARE(0x2A25),
                .access_cb = dis_str_access,
                .arg = (void *)s_serial_num,
                .flags = BLE_GATT_CHR_F_READ,
            },
            {
                .uuid = BLE_UUID16_DECLARE(0x2A26),
                .access_cb = dis_str_access,
                .arg = (void *)s_fw_rev,
                .flags = BLE_GATT_CHR_F_READ,
            },
            {
                .uuid = BLE_UUID16_DECLARE(0x2A27),
                .access_cb = dis_str_access,
                .arg = (void *)s_hw_rev,
                .flags = BLE_GATT_CHR_F_READ,
            },
            {0},
        },
    },
    {
        .type = BLE_GATT_SVC_TYPE_PRIMARY,
        .uuid = &SYN_SVC_UUID.u,
        .characteristics = (struct ble_gatt_chr_def[]){
            {
                .uuid = &CFG_UUID.u,
                .access_cb = cfg_access,
                .flags = BLE_GATT_CHR_F_READ | BLE_GATT_CHR_F_WRITE | BLE_GATT_CHR_F_WRITE_ENC,
            },
            {
                .uuid = &PROV_UUID.u,
                .access_cb = prov_access,
                .val_handle = &s_prov_handle,
                // Provisioning commands (COMMIT, FACTORY_RESET...) need an encrypted link: the
                // central must pair first (LE Secure Connections, Just Works: it keeps a passive
                // eavesdropper out; MITM protection needs an I/O capability the band does not have).
                .flags = BLE_GATT_CHR_F_WRITE | BLE_GATT_CHR_F_WRITE_ENC | BLE_GATT_CHR_F_NOTIFY,
            },
            {
                .uuid = &FEAT2_UUID.u,
                .access_cb = feat2_access,
                .val_handle = &s_feat2_handle,
                .flags = BLE_GATT_CHR_F_NOTIFY,
            },
            {
                .uuid = &STATUS_UUID.u,
                .access_cb = status_access,
                .val_handle = &s_status_handle,
                .flags = BLE_GATT_CHR_F_READ | BLE_GATT_CHR_F_NOTIFY,
            },
            {
                .uuid = &META_UUID.u,
                .access_cb = meta_access,
                .flags = BLE_GATT_CHR_F_READ,
            },
            {
                .uuid = &CTL_UUID.u,
                .access_cb = ctl_access,
                .flags = BLE_GATT_CHR_F_WRITE,
            },
            {0},
        },
    },
    {0},
};

// --- GAP -----------------------------------------------------------------------

/**
 * Ask the central for a link that carries the raw streams: a short connection interval, the
 * largest data length (251 B PDUs, so one 244 B notification is one packet) and the 2M PHY.
 * These are REQUESTS: the phone may refuse any of them (Android in the background, iOS limits),
 * and the band then works on what it got; a refusal is logged, never an error. The ATT MTU is
 * negotiated separately above.
 */
static void tune_link(uint16_t conn_handle) {
    struct ble_gap_upd_params params = {
        .itvl_min = 12,              // 15 ms (units of 1.25 ms)
        .itvl_max = 24,              // 30 ms
        .latency = 0,                // no skipped events: the streams are continuous
        .supervision_timeout = 400,  // 4 s (units of 10 ms)
        .min_ce_len = 0,
        .max_ce_len = 0,
    };
    int rc = ble_gap_update_params(conn_handle, &params);
    if (rc != 0) ESP_LOGI(BLE_TAG, "connection parameter request not sent: %d", rc);
    rc = ble_gap_set_data_len(conn_handle, 251, 2120);
    if (rc != 0) ESP_LOGI(BLE_TAG, "data length request not sent: %d", rc);
#if defined(CONFIG_BT_NIMBLE_LL_CFG_FEAT_LE_2M_PHY)
    rc = ble_gap_set_prefered_le_phy(conn_handle, BLE_GAP_LE_PHY_2M_MASK, BLE_GAP_LE_PHY_2M_MASK,
                                     BLE_GAP_LE_PHY_CODED_ANY);
    if (rc != 0) ESP_LOGI(BLE_TAG, "2M PHY request not sent: %d", rc);
#endif
}

static int gap_event_cb(struct ble_gap_event *event, void *arg) {
    (void)arg;
    switch (event->type) {
    case BLE_GAP_EVENT_CONNECT:
        if (event->connect.status == 0) {
            g_conn_handle = event->connect.conn_handle;
            g_connected = true;
            g_mtu = 23;  // the default until this link negotiates its own: never the previous peer's
            s_mtu_warned = false;
            // The research service owns its own connection handle: without
            // this the raw stream path had BLE_HS_CONN_HANDLE_NONE forever
            // and silently transmitted nothing (Fase 1, bloccante B1).
            synapse_research_service_on_connect(event->connect.conn_handle);
            // Ask for a larger ATT MTU ourselves: a central that does not (Android needs an
            // explicit request) would leave the 26/32-byte v2 frames and the raw streams stuck at
            // the 20-byte default. A central that also asks gets BLE_HS_EALREADY: harmless.
            (void)ble_gattc_exchange_mtu(g_conn_handle, NULL, NULL);
            tune_link(g_conn_handle);
            ESP_LOGI(BLE_TAG, "Connected handle=%d", g_conn_handle);
        } else {
            ESP_LOGW(BLE_TAG, "Connection failed: %d", event->connect.status);
            synapse_ble_gatt_start_advertising_normal();
        }
        return 0;
    case BLE_GAP_EVENT_DISCONNECT:
        ESP_LOGI(BLE_TAG, "Disconnected reason=%d", event->disconnect.reason);
        g_connected = false;
        g_conn_handle = BLE_HS_CONN_HANDLE_NONE;
        g_prov_subscribed = false;
        s_hr_subscribed = false;
        s_batt_subscribed = false;
        s_feat2_subscribed = false;
        s_status_subscribed = false;
        s_feat2_seq = 0;  // a new session starts a new sequence
        g_mtu = 23;
        s_mtu_warned = false;
        // A Stop written by one client must not silence the raw streams for the next one.
        if (s_transmit_handler != NULL) s_transmit_handler(true);
        s_feat2_rate_hz = SYNAPSE_CONTRACT_V2_FEATURE_RATE_HZ;
        synapse_research_service_on_disconnect();
#if defined(CONFIG_SYNAPSE_FEATURE_PROVISIONING)
        if (!synapse_provisioning_is_complete()) synapse_ble_gatt_start_advertising_provisioning();
        else synapse_ble_gatt_start_advertising_normal();
#else
        // band-core: provisioning is not part of the build, so the pod is
        // always in normal mode.
        synapse_ble_gatt_start_advertising_normal();
#endif
        return 0;
    case BLE_GAP_EVENT_SUBSCRIBE:
        ESP_LOGI(BLE_TAG, "Subscribe attr=%d notify=%d",
                 event->subscribe.attr_handle, event->subscribe.cur_notify);
        if (event->subscribe.attr_handle == s_prov_handle) {
            g_prov_subscribed = event->subscribe.cur_notify;
        } else if (event->subscribe.attr_handle == s_feat2_handle) {
            s_feat2_subscribed = event->subscribe.cur_notify;
        } else if (event->subscribe.attr_handle == s_status_handle) {
            s_status_subscribed = event->subscribe.cur_notify;
        } else if (event->subscribe.attr_handle == s_hr_handle) {
            s_hr_subscribed = event->subscribe.cur_notify;
        } else if (event->subscribe.attr_handle == s_batt_handle) {
            s_batt_subscribed = event->subscribe.cur_notify;
            if (s_batt_subscribed && s_batt_notify_pending) {
                s_batt_notify_pending = false;
                (void)synapse_ble_gatt_notify_battery(s_batt_level, false);
            }
        }
        // Route research-service CCCDs to the raw stream table: a stream only
        // transmits once its characteristic has been subscribed.
        synapse_research_service_handle_subscribe(event->subscribe.attr_handle,
                                                  event->subscribe.cur_notify);
        return 0;
    case BLE_GAP_EVENT_MTU:
        g_mtu = event->mtu.value;
        synapse_research_service_set_mtu(event->mtu.value);
        return 0;
    case BLE_GAP_EVENT_ADV_COMPLETE:
        ESP_LOGI(BLE_TAG, "Advertising complete");
        return 0;
    default:
        return 0;
    }
}

/* Device name broadcast by the band. The complete name goes into the scan
 * response: a legacy advertising packet is limited to 31 bytes and the
 * flags + 128-bit UUID already use 21 of them. */
#define SYNAPSE_BLE_DEVICE_NAME CONFIG_SYNAPSE_BLE_DEVICE_NAME  /* Kconfig: one name, not two */

/**
 * @brief Byte size of the advertising packet.
 *
 * flags (3 B) + the 128-bit Synapse consumer service UUID (18 B).
 */
uint8_t synapse_ble_gatt_adv_payload_size(void) {
    return (uint8_t)(SYNAPSE_BLE_ADV_FLAGS_BYTES + SYNAPSE_BLE_ADV_UUID128_BYTES);
}

/**
 * @brief Byte size of the scan response.
 *
 * Complete local name (2 B + name) + the 16-bit Heart Rate service UUID
 * (4 B), so a standard HR client still discovers the device by UUID.
 */
uint8_t synapse_ble_gatt_scan_response_size(void) {
    return (uint8_t)(SYNAPSE_BLE_ADV_NAME_BYTES(strlen(SYNAPSE_BLE_DEVICE_NAME))
                     + SYNAPSE_BLE_ADV_UUID16_BYTES);
}

/* The advertising packet and the scan response must both fit the 31-byte
 * legacy limit; a silent overflow would leave the device invisible. */
_Static_assert(SYNAPSE_BLE_ADV_FLAGS_BYTES + SYNAPSE_BLE_ADV_UUID128_BYTES
                   <= SYNAPSE_BLE_ADV_PAYLOAD_MAX,
               "advertising packet exceeds the 31-byte legacy limit");
_Static_assert(SYNAPSE_BLE_ADV_NAME_BYTES(sizeof(SYNAPSE_BLE_DEVICE_NAME) - 1)
                   + SYNAPSE_BLE_ADV_UUID16_BYTES <= SYNAPSE_BLE_ADV_PAYLOAD_MAX,
               "scan response exceeds the 31-byte legacy limit");

static void start_advertising_impl(const char *name) {
    struct ble_hs_adv_fields fields = {0};
    fields.flags = BLE_HS_ADV_F_DISC_GEN | BLE_HS_ADV_F_BREDR_UNSUP;
    // Advertise the Synapse consumer service so Averyn can find the device by
    // UUID without connecting first (phase 0 contract).
    static const ble_uuid128_t svc_uuid128 =
        SYNAPSE_CONTRACT_UUID_CONSUMER_SERVICE_INIT;
    fields.uuids128 = (ble_uuid128_t[]){svc_uuid128};
    fields.num_uuids128 = 1;
    fields.uuids128_is_complete = 1;

    uint8_t adv_bytes = synapse_ble_gatt_adv_payload_size();
    if (adv_bytes > SYNAPSE_BLE_ADV_PAYLOAD_MAX) {
        ESP_LOGE(BLE_TAG, "advertising payload too big: %u > %u bytes",
                 (unsigned)adv_bytes, (unsigned)SYNAPSE_BLE_ADV_PAYLOAD_MAX);
        return;
    }
    int rc = ble_gap_adv_set_fields(&fields);
    if (rc != 0) {
        ESP_LOGE(BLE_TAG, "ble_gap_adv_set_fields failed: %d", rc);
        return;
    }

    // The complete name and the 16-bit Heart Rate UUID go into the scan
    // response: the advertising packet has no room left for them.
    struct ble_hs_adv_fields rsp = {0};
    rsp.name = (uint8_t *)name;
    rsp.name_len = strlen(name);
    rsp.name_is_complete = 1;
    static const ble_uuid16_t svc_uuid16 = BLE_UUID16_INIT(0x180D);
    rsp.uuids16 = (ble_uuid16_t[]){svc_uuid16};
    rsp.num_uuids16 = 1;
    rsp.uuids16_is_complete = 1;

    uint8_t rsp_bytes = synapse_ble_gatt_scan_response_size();
    if (rsp_bytes > SYNAPSE_BLE_ADV_PAYLOAD_MAX) {
        ESP_LOGE(BLE_TAG, "scan response too big: %u > %u bytes",
                 (unsigned)rsp_bytes, (unsigned)SYNAPSE_BLE_ADV_PAYLOAD_MAX);
        return;
    }
    rc = ble_gap_adv_rsp_set_fields(&rsp);
    if (rc != 0) {
        ESP_LOGE(BLE_TAG, "ble_gap_adv_rsp_set_fields failed: %d", rc);
        return;
    }

    struct ble_gap_adv_params adv_params = {0};
    adv_params.conn_mode = BLE_GAP_CONN_MODE_UND;
    adv_params.disc_mode = BLE_GAP_DISC_MODE_GEN;
    adv_params.itvl_min = BLE_GAP_ADV_ITVL_MS(32);
    adv_params.itvl_max = BLE_GAP_ADV_ITVL_MS(64);
    rc = ble_gap_adv_start(BLE_OWN_ADDR_PUBLIC, NULL, BLE_HS_FOREVER,
                            &adv_params, gap_event_cb, NULL);
    if (rc != 0) {
        ESP_LOGE(BLE_TAG, "ble_gap_adv_start failed: %d", rc);
        return;
    }
    ESP_LOGI(BLE_TAG, "Advertising started (%s): adv=%u B rsp=%u B",
             name, (unsigned)adv_bytes, (unsigned)rsp_bytes);
}

static void on_sync(void) {
    int rc = ble_svc_gap_device_name_set(SYNAPSE_BLE_DEVICE_NAME);
    assert(rc == 0);
    ble_hs_cfg.sm_io_cap = BLE_SM_IO_CAP_NO_IO;
    ble_hs_cfg.sm_sc = 1;       // LE Secure Connections
    ble_hs_cfg.sm_mitm = 0;     // no I/O to confirm with: Just Works
    ble_hs_cfg.sm_bonding = 0;  // no key store is initialised: pair per connection
    // Provisioning and normal mode advertise the same identity: the device
    // name and the consumer service UUID. The difference is in what the
    // provisioning characteristic accepts, not in the advertisement, so the
    // two branches are kept explicit instead of duplicating the call.
#if defined(CONFIG_SYNAPSE_FEATURE_PROVISIONING)
    if (!synapse_provisioning_is_complete()) {
        ESP_LOGI(BLE_TAG, "advertising (provisioning mode)");
    } else {
        ESP_LOGI(BLE_TAG, "advertising (normal mode)");
    }
#else
    // band-core: no provisioning in this build, so the pod is always normal.
    ESP_LOGI(BLE_TAG, "advertising (normal mode: provisioning not in this build)");
#endif
    start_advertising_impl(SYNAPSE_BLE_DEVICE_NAME);
}

static void on_reset(int reason) {
    ESP_LOGE(BLE_TAG, "NimBLE reset: %d", reason);
}

static void host_task_fn(void *arg) {
    (void)arg;
    nimble_port_run();
}

// --- public API ------------------------------------------------------------------

esp_err_t synapse_ble_gatt_init(void) {
    esp_nimble_hci_init();
    nimble_port_init();

    ble_hs_cfg.sync_cb = on_sync;
    ble_hs_cfg.reset_cb = on_reset;

    ble_svc_gap_init();
    ble_svc_gatt_init();

    int rc = ble_gatts_count_cfg(gatt_svr_svcs);
    if (rc != 0) {
        ESP_LOGE(BLE_TAG, "GATT count cfg failed: %d", rc);
        return ESP_FAIL;
    }
    rc = ble_gatts_add_svcs(gatt_svr_svcs);
    if (rc != 0) {
        ESP_LOGE(BLE_TAG, "GATT add svcs failed: %d", rc);
        return ESP_FAIL;
    }

    ESP_LOGI(BLE_TAG, "BLE GATT init OK (NimBLE)");

    // The Synapse Research Service MUST be registered before the host task
    // starts: NimBLE calls ble_gatts_start() automatically when the host
    // starts, and from that moment ble_gatts_add_svcs() fails with
    // BLE_HS_EBUSY. Registering it later (as app_main used to do) either
    // aborts the boot or silently loses the dataset path.
#if defined(CONFIG_SYNAPSE_FEATURE_RESEARCH_SERVICE) && !defined(CONFIG_SYNAPSE_PRODUCT_MODE)
    rc = synapse_research_service_init();
    if (rc != ESP_OK) {
        ESP_LOGE(BLE_TAG, "research service registration failed: %s",
                 esp_err_to_name(rc));
        return ESP_FAIL;
    }
#else
    ESP_LOGI(BLE_TAG, "product build: raw research service is disabled");
#endif

    nimble_port_freertos_init(host_task_fn);
    return ESP_OK;
}

void synapse_ble_gatt_start_advertising_provisioning(void) {
    if (ble_hs_synced()) start_advertising_impl(SYNAPSE_BLE_DEVICE_NAME);
}

void synapse_ble_gatt_start_advertising_normal(void) {
    if (ble_hs_synced()) start_advertising_impl(SYNAPSE_BLE_DEVICE_NAME);
}

void synapse_ble_gatt_process_events(void) {
}

esp_err_t synapse_ble_gatt_notify_provisioning(const synapse_prov_packet_t *packet) {
    if (!g_connected || g_conn_handle == BLE_HS_CONN_HANDLE_NONE) return ESP_ERR_INVALID_STATE;
    if (!g_prov_subscribed || s_prov_handle == 0) return ESP_ERR_INVALID_STATE;
    struct os_mbuf *om = ble_hs_mbuf_from_flat(packet, sizeof(*packet));
    if (!om) return ESP_ERR_NO_MEM;
    int rc = ble_gatts_notify_custom(g_conn_handle, s_prov_handle, om);
    return rc == 0 ? ESP_OK : ESP_FAIL;
}

bool synapse_ble_gatt_is_connected(void) {
    return g_connected;
}

uint16_t synapse_ble_gatt_get_mtu(void) {
    return g_mtu;
}

void synapse_ble_gatt_set_device_info(const char *m, const char *model, const char *s,
                                      const char *fw, const char *hw) {
    if (m) {
        strncpy(s_mfr_name, m, sizeof(s_mfr_name) - 1);
        s_mfr_name[sizeof(s_mfr_name) - 1] = '\0';
    }
    if (model) {
        strncpy(s_model_num, model, sizeof(s_model_num) - 1);
        s_model_num[sizeof(s_model_num) - 1] = '\0';
    }
    if (s) {
        strncpy(s_serial_num, s, sizeof(s_serial_num) - 1);
        s_serial_num[sizeof(s_serial_num) - 1] = '\0';
    }
    if (fw) {
        strncpy(s_fw_rev, fw, sizeof(s_fw_rev) - 1);
        s_fw_rev[sizeof(s_fw_rev) - 1] = '\0';
    }
    if (hw) {
        strncpy(s_hw_rev, hw, sizeof(s_hw_rev) - 1);
        s_hw_rev[sizeof(s_hw_rev) - 1] = '\0';
    }
    ESP_LOGI(BLE_TAG, "DIS set: %s %s S:%s FW:%s HW:%s", s_mfr_name, s_model_num,
             s_serial_num, s_fw_rev, s_hw_rev);
}

void synapse_ble_gatt_update_battery(uint8_t level, bool charging) {
    s_batt_level = level;
    s_batt_known = true;
    s_batt_charging = charging;
    if (s_batt_notify_pending && g_connected && g_conn_handle != BLE_HS_CONN_HANDLE_NONE) {
        synapse_ble_gatt_notify_battery(level, charging);
    }
}

/**
 * @brief Mark battery level as changed and notify subscribers if connected.
 * Call from the power module whenever the measured level changes.
 */
void synapse_ble_gatt_battery_changed(uint8_t level, bool charging) {
    s_batt_notify_pending = true;
    synapse_ble_gatt_update_battery(level, charging);
}

esp_err_t synapse_ble_gatt_push_heart_rate(uint16_t hr_bpm) {
    static uint32_t s_last_hr_ms = 0;
    if (!g_connected || g_conn_handle == BLE_HS_CONN_HANDLE_NONE) return ESP_ERR_INVALID_STATE;
    if (hr_bpm == 0xFFFF) return ESP_OK;  // no valid measurement: 0x2A37 has no "not available" code
    uint32_t now_ms = (uint32_t)(esp_timer_get_time() / 1000);
    // Standard Heart Rate on its own 1 Hz gate: a plain HR client (level L0, the band as a strap)
    // subscribes to 0x2A37 only. The value is recorded even while nobody is subscribed, so a
    // client that READS the characteristic sees the same measurement a notification would carry.
    // RR is not sent here: it sits behind the rr_recording consent and travels in the v2 feature
    // frame, where the phone masks it; a generic HR client would receive it unmasked.
    if (now_ms - s_last_hr_ms < 1000u) return ESP_OK;
    s_last_hr_ms = now_ms;
    const esp_err_t rc = synapse_ble_gatt_notify_hr(hr_bpm, 0xFFFF);
    // Not subscribed is not a failure (the value is recorded for a READ); a refused notification
    // is retried on the next call instead of a second later.
    if (rc == ESP_ERR_NO_MEM || rc == ESP_FAIL) s_last_hr_ms = now_ms - 1000u;
    return ESP_OK;
}

static esp_err_t notify_flat(uint16_t handle, const uint8_t *buf, size_t n) {
    struct os_mbuf *om = ble_hs_mbuf_from_flat(buf, (uint16_t)n);
    if (om == NULL) return ESP_ERR_NO_MEM;
    return ble_gatts_notify_custom(g_conn_handle, handle, om) == 0 ? ESP_OK : ESP_FAIL;
}

static uint32_t s_feat2_last_ms = 0;

bool synapse_ble_gatt_feature_v2_ready(void) {
    if (!g_connected || g_conn_handle == BLE_HS_CONN_HANDLE_NONE) return false;
    if (!s_feat2_subscribed || s_feat2_handle == 0) return false;
    // Same MTU floor as push_feature_v2(): a frame that cannot be sent is not deliverable.
    return g_mtu >= SYNAPSE_CONTRACT_V2_FEATURE_BYTES + 3u;
}

bool synapse_ble_gatt_feature_v2_due(void) {
    if (!synapse_ble_gatt_feature_v2_ready()) return false;
    uint32_t now_ms = (uint32_t)(esp_timer_get_time() / 1000);
    return now_ms - s_feat2_last_ms >= 1000u / s_feat2_rate_hz;
}

esp_err_t synapse_ble_gatt_push_feature_v2(synapse_feature_v2_t *rec) {
    if (rec == NULL) return ESP_ERR_INVALID_ARG;
    uint32_t now_ms = (uint32_t)(esp_timer_get_time() / 1000);
    // ESP_ERR_NOT_FINISHED = nothing was queued and nothing is wrong (rate gate / no subscriber):
    // ESP_OK is reserved for a frame that really went to the stack, because the caller consumes
    // data (RR intervals) on it.
    if (now_ms - s_feat2_last_ms < 1000u / s_feat2_rate_hz) return ESP_ERR_NOT_FINISHED;
    if (!g_connected || g_conn_handle == BLE_HS_CONN_HANDLE_NONE) return ESP_ERR_INVALID_STATE;
    if (!s_feat2_subscribed || s_feat2_handle == 0) return ESP_ERR_NOT_FINISHED;  // nothing subscribed
    if (g_mtu < SYNAPSE_CONTRACT_V2_FEATURE_BYTES + 3u) {
        if (!s_mtu_warned) {
            s_mtu_warned = true;
            ESP_LOGW(BLE_TAG, "ATT MTU %u cannot carry the v2 feature frame (%u B + 3 needed): "
                              "the client must negotiate a larger MTU",
                     (unsigned)g_mtu, (unsigned)SYNAPSE_CONTRACT_V2_FEATURE_BYTES);
        }
        return ESP_ERR_INVALID_SIZE;
    }

    rec->sequence = s_feat2_seq;
    if (s_batt_known) {
        rec->battery_pct = s_batt_level;
        // CHARGING is meaningful only when the power monitor exists (contract: read it only
        // when the POWER bit of sensor_health is set); never set it from an unmeasured state.
        if (s_batt_charging && (rec->sensor_health & SYNAPSE_V2_HEALTH_POWER)) {
            rec->flags |= SYNAPSE_V2_FLAG_CHARGING;
        }
    }
    uint8_t buf[SYNAPSE_CONTRACT_V2_FEATURE_BYTES];
    size_t n = synapse_feature_v2_encode(rec, buf, sizeof(buf));
    if (n == 0) return ESP_FAIL;
    esp_err_t rc = notify_flat(s_feat2_handle, buf, n);
    if (rc == ESP_OK) {
        s_feat2_seq++;  // consumed only by a frame that was actually queued: no phantom gap
        s_feat2_last_ms = now_ms;
    }
    return rc;
}

esp_err_t synapse_ble_gatt_push_status(void) {
    static uint32_t s_last_ms = 0;
    uint32_t now_ms = (uint32_t)(esp_timer_get_time() / 1000);
    if (now_ms - s_last_ms < 1000u) return ESP_OK;
    if (!g_connected || g_conn_handle == BLE_HS_CONN_HANDLE_NONE) return ESP_ERR_INVALID_STATE;
    if (!s_status_subscribed || s_status_handle == 0) return ESP_OK;
    if (g_mtu < SYNAPSE_CONTRACT_V2_STATUS_BYTES + 3u) return ESP_ERR_INVALID_SIZE;
    uint8_t buf[SYNAPSE_CONTRACT_V2_STATUS_BYTES];
    size_t n = build_status(buf, sizeof(buf));
    if (n == 0) return ESP_FAIL;
    esp_err_t rc = notify_flat(s_status_handle, buf, n);
    if (rc == ESP_OK) s_last_ms = now_ms;
    return rc;
}

void synapse_ble_gatt_set_status_provider(synapse_ble_status_provider_t provider) {
    s_status_provider = provider;
}

void synapse_ble_gatt_set_transmit_handler(synapse_ble_transmit_handler_t handler) {
    s_transmit_handler = handler;
}

void synapse_ble_gatt_set_time_handler(synapse_ble_time_handler_t handler) {
    s_time_handler = handler;
}
