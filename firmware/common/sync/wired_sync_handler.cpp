#include "wired_sync_handler.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "driver/gpio.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <string.h>

static const char* TAG = "wired_sync";

#define MAX_INTERVAL_SAMPLES 100

// ISR for sync pulse detection (pod side)
static void IRAM_ATTR wired_sync_isr_handler(void* arg) {
    synapse_wired_sync_t* sync = (synapse_wired_sync_t*)arg;
    if (!sync || !sync->initialized) return;
    
    int64_t timestamp_us = esp_timer_get_time();
    sync->last_pulse_timestamp_us = timestamp_us;
    sync->pulses_received++;
    
    // Store interval for averaging
    static int64_t last_timestamp = 0;
    static float intervals[MAX_INTERVAL_SAMPLES] = {0};
    static uint8_t interval_idx = 0;
    
    if (last_timestamp > 0) {
        float interval_ms = (timestamp_us - last_timestamp) / 1000.0f;
        intervals[interval_idx] = interval_ms;
        interval_idx = (interval_idx + 1) % MAX_INTERVAL_SAMPLES;
        
        // Update running average
        float sum = 0;
        uint8_t count = 0;
        for (uint8_t i = 0; i < MAX_INTERVAL_SAMPLES; i++) {
            if (intervals[i] > 0) {
                sum += intervals[i];
                count++;
            }
        }
        if (count > 0) {
            sync->avg_interval_ms = sum / count;
        }
    }
    last_timestamp = timestamp_us;
    
    // Call user callback if registered
    if (sync->on_sync_pulse) {
        sync->on_sync_pulse(timestamp_us, sync->user_ctx);
    }
}

esp_err_t synapse_wired_sync_init(synapse_wired_sync_t* sync, synapse_sync_role_t role) {
    if (!sync) return ESP_ERR_INVALID_ARG;
    
    memset(sync, 0, sizeof(synapse_wired_sync_t));
    sync->gpio_pin = SYNAPSE_WIRED_SYNC_GPIO;
    sync->role = role;
    sync->pulse_width_us = SYNAPSE_WIRED_SYNC_PULSE_WIDTH_US;
    sync->pull_up = SYNAPSE_WIRED_SYNC_PULL_UP;
    
    gpio_config_t io_conf = {};
    io_conf.pin_bit_mask = (1ULL << sync->gpio_pin);
    
    if (role == SYNAPSE_SYNC_ROLE_HUB) {
        // Hub: Output mode, drive sync pulse
        io_conf.mode = GPIO_MODE_OUTPUT;
        io_conf.pull_up_en = GPIO_PULLUP_DISABLE;
        io_conf.pull_down_en = GPIO_PULLDOWN_DISABLE;
        io_conf.intr_type = GPIO_INTR_DISABLE;
        
        ESP_LOGI(TAG, "Initializing wired sync as HUB on GPIO %d", sync->gpio_pin);
    } else {
        // Pod: Input mode with interrupt on rising edge
        io_conf.mode = GPIO_MODE_INPUT;
        io_conf.pull_up_en = sync->pull_up ? GPIO_PULLUP_ENABLE : GPIO_PULLUP_DISABLE;
        io_conf.pull_down_en = GPIO_PULLDOWN_DISABLE;
        io_conf.intr_type = GPIO_INTR_POSEDGE;
        
        ESP_LOGI(TAG, "Initializing wired sync as POD on GPIO %d", sync->gpio_pin);
    }
    
    esp_err_t err = gpio_config(&io_conf);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "GPIO config failed: %s", esp_err_to_name(err));
        return err;
    }
    
    if (role == SYNAPSE_SYNC_ROLE_POD) {
        // Install ISR service if not already installed
        static bool isr_installed = false;
        if (!isr_installed) {
            err = gpio_install_isr_service(ESP_INTR_FLAG_IRAM | ESP_INTR_FLAG_LEVEL1);
            if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
                ESP_LOGE(TAG, "ISR service install failed: %s", esp_err_to_name(err));
                return err;
            }
            isr_installed = true;
        }
        
        // Add ISR handler
        err = gpio_isr_handler_add(sync->gpio_pin, wired_sync_isr_handler, (void*)sync);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "ISR handler add failed: %s", esp_err_to_name(err));
            return err;
        }
    }
    
    // Initialize GPIO to low (hub) or read initial state (pod)
    if (role == SYNAPSE_SYNC_ROLE_HUB) {
        gpio_set_level(sync->gpio_pin, 0);
    }
    
    sync->initialized = true;
    ESP_LOGI(TAG, "Wired sync initialized successfully (role: %s)", 
             role == SYNAPSE_SYNC_ROLE_HUB ? "HUB" : "POD");
    
    return ESP_OK;
}

esp_err_t synapse_wired_sync_send_pulse(synapse_wired_sync_t* sync) {
    if (!sync || !sync->initialized) return ESP_ERR_INVALID_STATE;
    if (sync->role != SYNAPSE_SYNC_ROLE_HUB) return ESP_ERR_INVALID_STATE;
    
    // Drive high for pulse_width_us
    gpio_set_level(sync->gpio_pin, 1);
    esp_rom_delay_us(sync->pulse_width_us);
    gpio_set_level(sync->gpio_pin, 0);
    
    sync->pulses_sent++;
    sync->last_pulse_timestamp_us = esp_timer_get_time();
    
    ESP_LOGD(TAG, "Sync pulse sent: seq=%" PRIu32 ", width=%" PRIu32 "us", 
             sync->pulses_sent, sync->pulse_width_us);
    
    return ESP_OK;
}

int64_t synapse_wired_sync_get_last_pulse(const synapse_wired_sync_t* sync) {
    if (!sync || !sync->initialized) return 0;
    return sync->last_pulse_timestamp_us;
}

void synapse_wired_sync_get_stats(const synapse_wired_sync_t* sync,
                                   uint32_t* sent, uint32_t* received,
                                   float* avg_interval_ms) {
    if (!sync) return;
    if (sent) *sent = sync->pulses_sent;
    if (received) *received = sync->pulses_received;
    if (avg_interval_ms) *avg_interval_ms = sync->avg_interval_ms;
}

esp_err_t synapse_wired_sync_deinit(synapse_wired_sync_t* sync) {
    if (!sync || !sync->initialized) return ESP_ERR_INVALID_STATE;
    
    if (sync->role == SYNAPSE_SYNC_ROLE_POD) {
        gpio_isr_handler_remove(sync->gpio_pin);
    }
    
    gpio_reset_pin(sync->gpio_pin);
    sync->initialized = false;
    
    ESP_LOGI(TAG, "Wired sync deinitialized");
    return ESP_OK;
}