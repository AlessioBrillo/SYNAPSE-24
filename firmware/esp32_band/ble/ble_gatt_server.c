/**
 * @file ble_gatt_server.c
 * @brief Synapse Band v1 - BLE GATT Server (NimBLE, ESP-IDF 5.x)
 * Same stack as head/forearm/common. Standard: HR (0x180D), Battery (0x180F),
 * Device Info (0x180A). Custom 128-bit Synapse service: feature stream (notify),
 * device config (R/W), provisioning (W/N).
 */

#include <string.h>
#include <assert.h>
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
#include "ble_gatt_server_priv.h"
#include "ble_research_service.h"
#include "synapse_contract_generated.h"
#if defined(CONFIG_SYNAPSE_FEATURE_PROVISIONING)
#include "synapse_provisioning.h"
#endif
#include "synapse_config.h"

static uint16_t g_conn_handle = BLE_HS_CONN_HANDLE_NONE;
static bool g_connected = false;
static bool g_feat_subscribed = false;
static bool g_prov_subscribed = false;
static uint8_t g_feat_seq = 0;
static uint16_t g_mtu = 23;

// Value handles are filled by NimBLE (val_handle) when the host starts the GATT
// server; they are 0 until then. ble_gatts_find_chr() returns an rc, not a handle.
static uint16_t s_feat_handle = 0;
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
// Feature stream: db4307e5-6cb3-4152-a0e2-81c3921168e9
static const ble_uuid128_t FEAT_UUID =
    SYNAPSE_CONTRACT_UUID_CONSUMER_FEATURE_INIT;
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
// The 100 default in s_batt_level is a placeholder, not a measurement: v2 reports
// the level only once the power module has actually provided one.
static bool s_batt_known = false;
static bool s_mtu_warned = false;  // one warning per connection when the MTU cannot carry v2
static bool s_batt_charging = false;

// --- characteristic access callbacks -----------------------------------------

// Feature stream: notify-only, never read.
static int feat_access(uint16_t conn, uint16_t attr, struct ble_gatt_access_ctxt *ctxt, void *arg) {
    (void)conn; (void)attr; (void)arg;
    return BLE_ATT_ERR_UNLIKELY;
}

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

static void ctl_set_feature_rate(uint8_t hz, void *ctx) {
    (void)ctx;
    s_feat2_rate_hz = hz;  // validated 1..SYNAPSE_CONTRACT_V2_FEATURE_RATE_MAX_HZ by the parser
}

static int ctl_access(uint16_t conn, uint16_t attr, struct ble_gatt_access_ctxt *ctxt, void *arg) {
    (void)conn; (void)attr; (void)arg;
    if (ctxt->op != BLE_GATT_ACCESS_OP_WRITE_CHR) return BLE_ATT_ERR_UNLIKELY;
    uint8_t req[8];
    uint16_t len = 0;
    if (ble_hs_mbuf_to_flat(ctxt->om, req, sizeof(req), &len) != 0) {
        return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;
    }
    synapse_control_ops_t ops = {
        .set_transmitting = ctl_set_transmitting,
        .set_feature_rate = ctl_set_feature_rate,
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
        uint8_t val[1 + 2 + 2]; // flags + HR16 (+ RR16 when present)
        uint8_t flags = 0x01;   // bit0: 16-bit Heart Rate Measurement Value
        uint16_t rr = s_last_rr_ms;
        if (rr != 0xFFFF) flags |= 0x10; // RR interval present
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

static int cfg_access(uint16_t conn, uint16_t attr, struct ble_gatt_access_ctxt *ctxt, void *arg) {
    (void)conn; (void)attr; (void)arg;
    if (ctxt->op == BLE_GATT_ACCESS_OP_READ_CHR) {
        int rc = os_mbuf_append(ctxt->om, s_cfg_buf, s_cfg_len);
        return rc == 0 ? 0 : BLE_ATT_ERR_INSUFFICIENT_RES;
    }
    if (ctxt->op == BLE_GATT_ACCESS_OP_WRITE_CHR) {
        uint16_t len = OS_MBUF_PKTLEN(ctxt->om);
        if (len > sizeof(s_cfg_buf)) return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;
        s_cfg_len = len;
        os_mbuf_copydata(ctxt->om, 0, len, s_cfg_buf);
        synapse_config_update_from_ble(s_cfg_buf, s_cfg_len);
        return 0;
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
    if (s_hr_handle == 0 || !s_hr_subscribed) return ESP_ERR_INVALID_STATE;
    // Standard 0x2A37 flags: bit0 = 16-bit HR value, bit4 = RR interval present.
    uint8_t flags = 0x01; // 16-bit HR value
    if (rr_interval_ms != 0xFFFF) flags |= 0x10; // RR interval present
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
        uint16_t len = OS_MBUF_PKTLEN(ctxt->om);
        if (len < 4) return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;
        uint8_t hdr[4];
        os_mbuf_copydata(ctxt->om, 0, 4, hdr);
        uint16_t data_len = hdr[2] | ((uint16_t)hdr[3] << 8);
        uint8_t payload[240];
        if (data_len > sizeof(payload)) return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;
        if (data_len) os_mbuf_copydata(ctxt->om, 4, data_len, payload);
#if defined(CONFIG_SYNAPSE_FEATURE_PROVISIONING)
        synapse_provisioning_process_cmd(hdr[0], payload, data_len);
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
                .uuid = &FEAT_UUID.u,
                .access_cb = feat_access,
                .val_handle = &s_feat_handle,
                .flags = BLE_GATT_CHR_F_NOTIFY,
            },
            {
                .uuid = &CFG_UUID.u,
                .access_cb = cfg_access,
                .flags = BLE_GATT_CHR_F_READ | BLE_GATT_CHR_F_WRITE,
            },
            {
                .uuid = &PROV_UUID.u,
                .access_cb = prov_access,
                .val_handle = &s_prov_handle,
                .flags = BLE_GATT_CHR_F_WRITE | BLE_GATT_CHR_F_NOTIFY,
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

static int gap_event_cb(struct ble_gap_event *event, void *arg) {
    (void)arg;
    switch (event->type) {
    case BLE_GAP_EVENT_CONNECT:
        if (event->connect.status == 0) {
            g_conn_handle = event->connect.conn_handle;
            g_connected = true;
            g_feat_seq = 0;
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
        g_feat_subscribed = false;
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
        if (event->subscribe.attr_handle == s_feat_handle) {
            g_feat_subscribed = event->subscribe.cur_notify;
        } else if (event->subscribe.attr_handle == s_prov_handle) {
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
#define SYNAPSE_BLE_DEVICE_NAME "SYNAPSE-BAND"

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
    rc = synapse_research_service_init();
    if (rc != ESP_OK) {
        ESP_LOGE(BLE_TAG, "research service registration failed: %s",
                 esp_err_to_name(rc));
        return ESP_FAIL;
    }

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

esp_err_t synapse_ble_gatt_notify_features(const synapse_feature_stream_t *feature) {
    if (!g_connected || g_conn_handle == BLE_HS_CONN_HANDLE_NONE) return ESP_ERR_INVALID_STATE;
    if (!g_feat_subscribed || s_feat_handle == 0) return ESP_ERR_INVALID_STATE;
    synapse_feature_stream_t p = *feature;
    p.sequence = g_feat_seq++;
    struct os_mbuf *om = ble_hs_mbuf_from_flat(&p, sizeof(p));
    if (!om) return ESP_ERR_NO_MEM;
    int rc = ble_gatts_notify_custom(g_conn_handle, s_feat_handle, om);
    return rc == 0 ? ESP_OK : ESP_FAIL;
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

esp_err_t synapse_ble_gatt_push_feature(uint16_t hr_bpm, uint16_t rr_interval_ms,
                                         uint8_t ppg_sqi, uint8_t ppg_map,
                                         uint8_t ecg_quality, uint8_t stress_level,
                                         uint8_t motion_level, int16_t temperature) {
    static uint32_t s_last_notify_ms = 0;
    static uint32_t s_last_hr_ms = 0;
    uint32_t now_ms = (uint32_t)(esp_timer_get_time() / 1000);
    if (!g_connected || g_conn_handle == BLE_HS_CONN_HANDLE_NONE) return ESP_ERR_INVALID_STATE;

    // Standard Heart Rate first, on its own 1 Hz gate: a plain HR client (level L0, the band as a
    // strap) subscribes to 0x2A37 only and never to the feature characteristic. It used to be
    // mirrored from inside the feature path, so that client received nothing.
    // rr_interval_ms is reserved for genuine RR intervals (not RMSSD): 0xFFFF unless a real
    // RR is available.
    if (hr_bpm != 0xFFFF && s_hr_subscribed && now_ms - s_last_hr_ms >= 1000) {
        if (synapse_ble_gatt_notify_hr(hr_bpm, 0xFFFF) == ESP_OK) s_last_hr_ms = now_ms;
    }

    // 1 Hz default gate: 1000 ms between notifications. Configurable later via
    // the config characteristic if Averyn needs a faster live feed.
    if (now_ms - s_last_notify_ms < 1000) return ESP_OK;
    if (!g_feat_subscribed || s_feat_handle == 0) return ESP_OK; // nothing subscribed: silent drop

    synapse_feature_stream_t f = {0};
    f.version = SYNAPSE_FEATURE_STREAM_VERSION;
    f.sequence = g_feat_seq++;
    f.heart_rate = hr_bpm;
    f.rr_interval_ms = rr_interval_ms;
    f.ppg_sqi = ppg_sqi;
    f.ppg_map = ppg_map;
    f.ecg_quality = ecg_quality;
    f.stress_level = stress_level;
    f.motion_level = motion_level;
    // v1 has no "not available" for these: 0xFF is out of range (0..100 %), which a v1 parser
    // reads as null; a placeholder 100 % would read as a full battery that nobody measured.
    f.battery_level = s_batt_known ? s_batt_level : 0xFF;
    f.charging = (s_batt_known && s_batt_charging) ? 1 : 0;
    f.sport_type = 0;
    f.cadence = 0xFFFF;
    f.temperature = temperature;
    f.rmssd_ms = 0xFFFF;
    f.sdnn_ms = 0xFFFF;

    esp_err_t rc = synapse_ble_gatt_notify_features(&f);
    if (rc != ESP_OK) return rc;

    s_last_notify_ms = now_ms;
    return ESP_OK;
}

static esp_err_t notify_flat(uint16_t handle, const uint8_t *buf, size_t n) {
    struct os_mbuf *om = ble_hs_mbuf_from_flat(buf, (uint16_t)n);
    if (om == NULL) return ESP_ERR_NO_MEM;
    return ble_gatts_notify_custom(g_conn_handle, handle, om) == 0 ? ESP_OK : ESP_FAIL;
}

esp_err_t synapse_ble_gatt_push_feature_v2(synapse_feature_v2_t *rec) {
    static uint32_t s_last_ms = 0;
    if (rec == NULL) return ESP_ERR_INVALID_ARG;
    uint32_t now_ms = (uint32_t)(esp_timer_get_time() / 1000);
    if (now_ms - s_last_ms < 1000u / s_feat2_rate_hz) return ESP_OK;
    if (!g_connected || g_conn_handle == BLE_HS_CONN_HANDLE_NONE) return ESP_ERR_INVALID_STATE;
    if (!s_feat2_subscribed || s_feat2_handle == 0) return ESP_OK;  // nothing subscribed
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
        if (s_batt_charging) rec->flags |= SYNAPSE_V2_FLAG_CHARGING;
    }
    uint8_t buf[SYNAPSE_CONTRACT_V2_FEATURE_BYTES];
    size_t n = synapse_feature_v2_encode(rec, buf, sizeof(buf));
    if (n == 0) return ESP_FAIL;
    esp_err_t rc = notify_flat(s_feat2_handle, buf, n);
    if (rc == ESP_OK) {
        s_feat2_seq++;  // consumed only by a frame that was actually queued: no phantom gap
        s_last_ms = now_ms;
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
