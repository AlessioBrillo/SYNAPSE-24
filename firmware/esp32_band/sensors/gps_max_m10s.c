/**
 * @file gps_max_m10s.c
 * @brief GPS MAX-M10S UBX Protocol Implementation
 */

#include <string.h>
#include <stdlib.h>
#include <math.h>
#include <time.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "driver/uart.h"
#include "driver/gpio.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "gps_max_m10s.h"

static const char *TAG = "GPS_MAX_M10S";

static gps_max_m10s_t g_gps = {0};

// UBX checksum calculation
static void ubx_checksum(const uint8_t *data, size_t len, uint8_t *ck_a, uint8_t *ck_b) {
    uint8_t a = 0, b = 0;
    for (size_t i = 0; i < len; i++) {
        a += data[i];
        b += a;
    }
    *ck_a = a;
    *ck_b = b;
}

// Parse UBX-NAV-PVT payload (92 bytes)
static bool parse_nav_pvt(const uint8_t *payload, gps_data_t *data) {
    if (!payload || !data) return false;

    // iTOW (4 bytes) - GPS time of week in ms
    uint32_t itow = payload[0] | (payload[1] << 8) | (payload[2] << 16) | (payload[3] << 24);
    
    // year (2 bytes), month (1), day (1)
    uint16_t year = payload[4] | (payload[5] << 8);
    uint8_t month = payload[6];
    uint8_t day = payload[7];
    
    // hour (1), min (1), sec (1), valid (1)
    uint8_t hour = payload[8];
    uint8_t min = payload[9];
    uint8_t sec = payload[10];
    uint8_t valid = payload[11];
    
    // tAcc (4), nano (4) - skip for now
    
    // fixType (1), flags (1), flags2 (1), numSV (1)
    uint8_t fix_type = payload[20];
    uint8_t flags = payload[21];
    uint8_t num_sv = payload[23];
    
    // lon (4), lat (4), height (4), hMSL (4) - in 1e-7 degrees and mm
    int32_t lon = (int32_t)(payload[24] | (payload[25] << 8) | (payload[26] << 16) | (payload[27] << 24));
    int32_t lat = (int32_t)(payload[28] | (payload[29] << 8) | (payload[30] << 16) | (payload[31] << 24));
    int32_t height = (int32_t)(payload[32] | (payload[33] << 8) | (payload[34] << 16) | (payload[35] << 24));
    int32_t h_msl = (int32_t)(payload[36] | (payload[37] << 8) | (payload[38] << 16) | (payload[39] << 24));
    
    // hAcc (4), vAcc (4) - skip
    
    // velN (4), velE (4), velD (4), gSpeed (4) - mm/s
    int32_t vel_n = (int32_t)(payload[44] | (payload[45] << 8) | (payload[46] << 16) | (payload[47] << 24));
    int32_t vel_e = (int32_t)(payload[48] | (payload[49] << 8) | (payload[50] << 16) | (payload[51] << 24));
    int32_t vel_d = (int32_t)(payload[52] | (payload[53] << 8) | (payload[54] << 16) | (payload[55] << 24));
    int32_t g_speed = (int32_t)(payload[56] | (payload[57] << 8) | (payload[58] << 16) | (payload[59] << 24));
    
    // headMot (4) - heading of motion (1e-5 deg)
    int32_t head_mot = (int32_t)(payload[60] | (payload[61] << 8) | (payload[62] << 16) | (payload[63] << 24));
    
    // sAcc (4), headAcc (4) - skip
    // pDOP (2)
    uint16_t pdop = payload[74] | (payload[75] << 8);
    
    // flags3 (1), reserved (5), headVeh (4) - skip

    // Fill data struct
    data->valid = (valid & 0x01) && (fix_type >= 2); // Valid time + at least 2D fix
    data->has_fix = (fix_type >= 3); // 3D fix
    data->fix_type = fix_type;
    data->latitude = lat * 1e-7;
    data->longitude = lon * 1e-7;
    data->altitude = h_msl * 1e-3; // mm to meters
    data->speed_mps = g_speed * 1e-3; // mm/s to m/s
    data->heading_deg = head_mot * 1e-5; // 1e-5 deg to deg
    data->hdop = pdop * 0.01; // 0.01 scaling
    data->num_satellites = num_sv;
    
    // Convert GPS time to UTC timestamp (simplified)
    // GPS week rollover not handled - assumes current epoch
    struct tm tm = {0};
    tm.tm_year = year - 1900;
    tm.tm_mon = month - 1;
    tm.tm_mday = day;
    tm.tm_hour = hour;
    tm.tm_min = min;
    tm.tm_sec = sec;
    time_t utc_time = mktime(&tm);
    data->timestamp_us = (int64_t)utc_time * 1000000 + (itow % 1000) * 1000;
    data->local_timestamp_us = esp_timer_get_time();
    
    return true;
}

// Parse UBX-NAV-STATUS payload
static bool parse_nav_status(const uint8_t *payload, gps_data_t *data) {
    // gpsFix (1), flags (1), fixStat (1), flags2 (1), ttff (4), msss (4)
    uint8_t gps_fix = payload[0];
    data->has_fix = (gps_fix >= 3);
    return true;
}

// Parser task
static void gps_parser_task(void *arg) {
    gps_max_m10s_t *gps = &g_gps;
    uint8_t *rx_buffer = malloc(gps->config.rx_buffer_size);
    if (!rx_buffer) {
        ESP_LOGE(TAG, "Failed to allocate RX buffer");
        vTaskDelete(NULL);
        return;
    }

    ESP_LOGI(TAG, "GPS parser task started");

    while (gps->running) {
        int len = uart_read_bytes(gps->config.uart_port, rx_buffer, gps->config.rx_buffer_size - 1, pdMS_TO_TICKS(100));
        
        if (len > 0) {
            for (int i = 0; i < len; i++) {
                uint8_t byte = rx_buffer[i];
                
                switch (gps->parse_state) {
                    case UBX_STATE_SYNC1:
                        if (byte == UBX_SYNC_CHAR_1) gps->parse_state = UBX_STATE_SYNC2;
                        break;
                    case UBX_STATE_SYNC2:
                        if (byte == UBX_SYNC_CHAR_2) gps->parse_state = UBX_STATE_CLASS;
                        else gps->parse_state = UBX_STATE_SYNC1;
                        break;
                    case UBX_STATE_CLASS:
                        gps->msg_class = byte;
                        gps->parse_state = UBX_STATE_ID;
                        break;
                    case UBX_STATE_ID:
                        gps->msg_id = byte;
                        gps->parse_state = UBX_STATE_LENGTH_L;
                        break;
                    case UBX_STATE_LENGTH_L:
                        gps->payload_length = byte;
                        gps->parse_state = UBX_STATE_LENGTH_H;
                        break;
                    case UBX_STATE_LENGTH_H:
                        gps->payload_length |= (byte << 8);
                        gps->payload_index = 0;
                        if (gps->payload_length > UBX_MAX_PAYLOAD) {
                            gps->parse_state = UBX_STATE_SYNC1;
                            gps->sync_errors++;
                        } else if (gps->payload_length == 0) {
                            gps->parse_state = UBX_STATE_CK_A;
                        } else {
                            gps->parse_state = UBX_STATE_PAYLOAD;
                        }
                        break;
                    case UBX_STATE_PAYLOAD:
                        gps->payload[gps->payload_index++] = byte;
                        if (gps->payload_index >= gps->payload_length) {
                            gps->parse_state = UBX_STATE_CK_A;
                        }
                        break;
                    case UBX_STATE_CK_A:
                        gps->ck_a = byte;
                        gps->parse_state = UBX_STATE_CK_B;
                        break;
                    case UBX_STATE_CK_B: {
                        gps->ck_b = byte;
                        
                        // Verify checksum
                        uint8_t calc_a, calc_b;
                        uint8_t chk_data[6 + UBX_MAX_PAYLOAD];
                        chk_data[0] = gps->msg_class;
                        chk_data[1] = gps->msg_id;
                        chk_data[2] = gps->payload_length & 0xFF;
                        chk_data[3] = (gps->payload_length >> 8) & 0xFF;
                        memcpy(&chk_data[4], gps->payload, gps->payload_length);
                        ubx_checksum(chk_data, 4 + gps->payload_length, &calc_a, &calc_b);
                        
                        if (calc_a == gps->ck_a && calc_b == gps->ck_b) {
                            // Valid message - parse based on class/ID
                            if (gps->msg_class == UBX_CLASS_NAV) {
                                if (gps->msg_id == UBX_NAV_PVT) {
                                    if (parse_nav_pvt(gps->payload, &gps->latest_data)) {
                                        if (xSemaphoreTake(gps->data_mutex, pdMS_TO_TICKS(10)) == pdTRUE) {
                                            // Already updated in parse_nav_pvt
                                            xSemaphoreGive(gps->data_mutex);
                                        }
                                        gps->messages_parsed++;
                                    }
                                } else if (gps->msg_id == UBX_NAV_STATUS) {
                                    parse_nav_status(gps->payload, &gps->latest_data);
                                }
                            }
                        } else {
                            gps->crc_errors++;
                        }
                        gps->parse_state = UBX_STATE_SYNC1;
                        break;
                    }
                    default:
                        gps->parse_state = UBX_STATE_SYNC1;
                        break;
                }
            }
        }
    }

    free(rx_buffer);
    vTaskDelete(NULL);
}

// Send UBX message
static esp_err_t send_ubx_message(uint8_t msg_class, uint8_t msg_id, 
                                   const uint8_t *payload, uint16_t payload_len) {
    uint8_t header[6] = {UBX_SYNC_CHAR_1, UBX_SYNC_CHAR_2, msg_class, msg_id,
                         payload_len & 0xFF, (payload_len >> 8) & 0xFF};
    
    uint8_t ck_a, ck_b;
    ubx_checksum(&header[2], 4 + payload_len, &ck_a, &ck_b);
    
    uart_write_bytes(g_gps.config.uart_port, (const char*)header, 6);
    if (payload_len > 0) {
        uart_write_bytes(g_gps.config.uart_port, (const char*)payload, payload_len);
    }
    uint8_t cks[2] = {ck_a, ck_b};
    uart_write_bytes(g_gps.config.uart_port, (const char*)cks, 2);
    
    return ESP_OK;
}

// Wait for UBX-ACK
static esp_err_t wait_for_ack(uint8_t msg_class, uint8_t msg_id, uint32_t timeout_ms) {
    TickType_t start = xTaskGetTickCount();
    TickType_t timeout = pdMS_TO_TICKS(timeout_ms);
    
    while (xTaskGetTickCount() - start < timeout) {
        // In a real implementation, we'd parse ACK/NAK messages here
        // For now, just delay
        vTaskDelay(pdMS_TO_TICKS(50));
    }
    return ESP_OK; // Simplified
}

// Public API implementations
esp_err_t gps_max_m10s_init(const gps_max_m10s_config_t *config) {
    if (g_gps.initialized) {
        ESP_LOGW(TAG, "Already initialized");
        return ESP_OK;
    }
    if (!config) return ESP_ERR_INVALID_ARG;

    memcpy(&g_gps.config, config, sizeof(gps_max_m10s_config_t));
    
    // Configure UART
    uart_config_t uart_cfg = {
        .baud_rate = config->baudrate,
        .data_bits = UART_DATA_8_BITS,
        .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
        .rx_flow_ctrl_thresh = 0,
        .source_clk = UART_SCLK_DEFAULT,
    };
    
    ESP_ERROR_CHECK(uart_param_config(config->uart_port, &uart_cfg));
    ESP_ERROR_CHECK(uart_set_pin(config->uart_port, config->tx_gpio, config->rx_gpio, 
                                  UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE));
    ESP_ERROR_CHECK(uart_driver_install(config->uart_port, config->rx_buffer_size * 2, 
                                         0, 0, NULL, 0));
    
    // Create mutex
    g_gps.data_mutex = xSemaphoreCreateMutex();
    if (!g_gps.data_mutex) {
        ESP_LOGE(TAG, "Failed to create mutex");
        return ESP_ERR_NO_MEM;
    }
    
    // Initialize latest data
    memset(&g_gps.latest_data, 0, sizeof(gps_data_t));
    
    g_gps.initialized = true;
    ESP_LOGI(TAG, "GPS MAX-M10S initialized on UART%d (TX=%d, RX=%d, %d baud)", 
             config->uart_port, config->tx_gpio, config->rx_gpio, config->baudrate);
    return ESP_OK;
}

esp_err_t gps_max_m10s_start(void) {
    if (!g_gps.initialized) return ESP_ERR_INVALID_STATE;
    if (g_gps.running) return ESP_OK;
    
    g_gps.running = true;
    g_gps.parse_state = UBX_STATE_SYNC1;
    
    // Configure GPS for UBX protocol, 1Hz PVT
    if (g_gps.config.use_ubx) {
        // Disable NMEA on all ports
        uint8_t disable_nmea[] = {0xF0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x01};
        send_ubx_message(UBX_CLASS_CFG, UBX_CFG_MSG, disable_nmea, sizeof(disable_nmea));
        
        // Enable UBX-NAV-PVT at rate 1
        uint8_t enable_pvt[] = {0x01, 0x07, 0x01}; // NAV class, PVT id, rate 1
        send_ubx_message(UBX_CLASS_CFG, UBX_CFG_MSG, enable_pvt, sizeof(enable_pvt));
        
        // Set navigation rate to 1Hz
        uint8_t nav_rate[] = {0xE8, 0x03, 0x01, 0x00, 0x01, 0x00}; // 1000ms, 1 cycle, UTC
        send_ubx_message(UBX_CLASS_CFG, UBX_CFG_RATE, nav_rate, sizeof(nav_rate));
    }
    
    BaseType_t ret = xTaskCreate(gps_parser_task, "gps_parser", 4096, NULL, 5, &g_gps.parser_task);
    if (ret != pdPASS) {
        ESP_LOGE(TAG, "Failed to create parser task");
        g_gps.running = false;
        return ESP_ERR_NO_MEM;
    }
    
    ESP_LOGI(TAG, "GPS started");
    return ESP_OK;
}

esp_err_t gps_max_m10s_stop(void) {
    if (!g_gps.running) return ESP_OK;
    
    g_gps.running = false;
    if (g_gps.parser_task) {
        vTaskDelay(pdMS_TO_TICKS(200)); // Wait for task to exit
        g_gps.parser_task = NULL;
    }
    
    ESP_LOGI(TAG, "GPS stopped");
    return ESP_OK;
}

esp_err_t gps_max_m10s_deinit(void) {
    gps_max_m10s_stop();
    
    if (g_gps.data_mutex) {
        vSemaphoreDelete(g_gps.data_mutex);
        g_gps.data_mutex = NULL;
    }
    
    uart_driver_delete(g_gps.config.uart_port);
    g_gps.initialized = false;
    
    ESP_LOGI(TAG, "GPS deinitialized");
    return ESP_OK;
}

esp_err_t gps_max_m10s_get_latest(gps_data_t *data) {
    if (!data) return ESP_ERR_INVALID_ARG;
    if (!g_gps.initialized) return ESP_ERR_INVALID_STATE;
    
    if (xSemaphoreTake(g_gps.data_mutex, pdMS_TO_TICKS(10)) == pdTRUE) {
        memcpy(data, &g_gps.latest_data, sizeof(gps_data_t));
        xSemaphoreGive(g_gps.data_mutex);
        return data->valid ? ESP_OK : ESP_ERR_INVALID_STATE;
    }
    return ESP_ERR_TIMEOUT;
}

bool gps_max_m10s_has_fix(void) {
    if (!g_gps.initialized) return false;
    bool fix = false;
    if (xSemaphoreTake(g_gps.data_mutex, pdMS_TO_TICKS(10)) == pdTRUE) {
        fix = g_gps.latest_data.has_fix;
        xSemaphoreGive(g_gps.data_mutex);
    }
    return fix;
}

esp_err_t gps_max_m10s_set_message_rate(uint8_t msg_class, uint8_t msg_id, uint8_t rate) {
    uint8_t payload[] = {msg_class, msg_id, rate};
    return send_ubx_message(UBX_CLASS_CFG, UBX_CFG_MSG, payload, sizeof(payload));
}

esp_err_t gps_max_m10s_set_nav_rate(uint16_t meas_rate_ms, uint16_t nav_rate, uint16_t time_ref) {
    uint8_t payload[6];
    payload[0] = meas_rate_ms & 0xFF;
    payload[1] = (meas_rate_ms >> 8) & 0xFF;
    payload[2] = nav_rate & 0xFF;
    payload[3] = (nav_rate >> 8) & 0xFF;
    payload[4] = time_ref & 0xFF;
    payload[5] = (time_ref >> 8) & 0xFF;
    return send_ubx_message(UBX_CLASS_CFG, UBX_CFG_RATE, payload, sizeof(payload));
}

esp_err_t gps_max_m10s_send_command(uint8_t msg_class, uint8_t msg_id, 
                                     const uint8_t *payload, uint16_t payload_len,
                                     uint32_t timeout_ms) {
    esp_err_t ret = send_ubx_message(msg_class, msg_id, payload, payload_len);
    if (ret != ESP_OK) return ret;
    return wait_for_ack(msg_class, msg_id, timeout_ms);
}

esp_err_t gps_max_m10s_get_stats(uint32_t *parsed, uint32_t *crc_errors, uint32_t *sync_errors) {
    if (parsed) *parsed = g_gps.messages_parsed;
    if (crc_errors) *crc_errors = g_gps.crc_errors;
    if (sync_errors) *sync_errors = g_gps.sync_errors;
    return ESP_OK;
}