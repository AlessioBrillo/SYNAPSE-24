/**
 * @file synapse_acquisition.c
 * @brief Synapse Band v1 - Tiered Acquisition FSM Implementation
 * 
 * Implements T0/T1/T2 state machine per Architecture.md §33-43.
 * For Band v1: T0=Continuous ECG+PPG+IMU+GPS+Temp, T1=Sleep/Rest enhanced, T2=Sport sessions
 */

#include <string.h>
#include <time.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "synapse_acquisition.h"
#include "synapse_config.h"
#include "synapse_sensors.h"
#include "synapse_signal_quality.h"
#include "synapse_power.h"
#include "fit_writer.h"

static const char *TAG = "SYNAPSE_ACQ";

static synapse_acquisition_t g_acq = {0};

// Session callback
static void (*g_session_callback)(const acq_session_t *session, void *user_data) = NULL;
static void *g_session_user_data = NULL;

// FSM task period
#define ACQ_FSM_PERIOD_MS 1000  // 1Hz tick

// Forward declarations
static void fsm_task_fn(void *arg);
static esp_err_t transition_to_tier(acq_tier_t new_tier);
static void handle_tier_0(void);
static void handle_tier_1(void);
static void handle_tier_2(void);
static void update_night_window(void);
static void check_tier_promotion(void);
static void check_tier_demotion(void);
static esp_err_t start_session_internal(const acq_session_config_t *config);
static esp_err_t stop_session_internal(void);
static time_t get_utc_time(void);

esp_err_t synapse_acquisition_init(EventGroupHandle_t system_events) {
    if (g_acq.running) {
        ESP_LOGW(TAG, "Already initialized");
        return ESP_OK;
    }

    // Load hardware config for thresholds
    synapse_hw_config_t hw_cfg;
    ESP_ERROR_CHECK(synapse_config_get(&hw_cfg));

    // Initialize FSM state
    memset(&g_acq, 0, sizeof(synapse_acquisition_t));
    g_acq.system_events = system_events;
    g_acq.current_tier = ACQ_TIER_0;
    g_acq.target_tier = ACQ_TIER_0;
    
    // Motion gate config
    g_acq.sqi_min = hw_cfg.sqi_min;
    g_acq.map_max = hw_cfg.map_max;
    g_acq.required_consecutive_clean = hw_cfg.required_consecutive_clean;
    g_acq.motion_gate_armed = false;
    g_acq.consecutive_clean_windows = 0;
    
    // Immobility config
    g_acq.immobility_window_s = 30;
    g_acq.accel_threshold_g = 0.02f;
    g_acq.required_immobility_windows = 10;  // 5 minutes at 30s windows
    g_acq.immobility_counter = 0;
    g_acq.last_motion_time_us = esp_timer_get_time();
    
    // Night window config
    g_acq.sleep_start_hour = 23;
    g_acq.sleep_end_hour = 7;
    g_acq.night_window_active = false;
    
    // Power budget
    g_acq.power_t1_affordable = true;
    g_acq.power_t2_affordable = true;
    g_acq.tier1_h_used_today = 0;
    g_acq.tier2_min_used_today = 0;

    ESP_LOGI(TAG, "Acquisition FSM initialized (Tier 0 continuous)");
    return ESP_OK;
}

esp_err_t synapse_acquisition_start(void) {
    if (g_acq.running) return ESP_OK;
    
    g_acq.running = true;
    
    BaseType_t ret = xTaskCreate(fsm_task_fn, "acq_fsm", 4096, NULL, 4, &g_acq.fsm_task);
    if (ret != pdPASS) {
        ESP_LOGE(TAG, "Failed to create FSM task");
        g_acq.running = false;
        return ESP_ERR_NO_MEM;
    }
    
    // Signal Tier 0 active
    if (g_acq.system_events) {
        xEventGroupSetBits(g_acq.system_events, ACQ_EVT_T0_ACTIVE);
    }
    
    ESP_LOGI(TAG, "Acquisition FSM started");
    return ESP_OK;
}

esp_err_t synapse_acquisition_stop(void) {
    if (!g_acq.running) return ESP_OK;
    
    g_acq.running = false;
    if (g_acq.fsm_task) {
        vTaskDelay(pdMS_TO_TICKS(200));
        g_acq.fsm_task = NULL;
    }
    
    // Stop any active session
    if (g_acq.session_active) {
        stop_session_internal();
    }
    
    ESP_LOGI(TAG, "Acquisition FSM stopped");
    return ESP_OK;
}

esp_err_t synapse_acquisition_deinit(void) {
    synapse_acquisition_stop();
    memset(&g_acq, 0, sizeof(synapse_acquisition_t));
    return ESP_OK;
}

void synapse_acquisition_tick(void) {
    // This is called from main loop (every 10ms)
    // The actual FSM runs in its own task at 1Hz
    // This function can be used for time-critical updates if needed
}

// ============================================================================
// Public API
// ============================================================================

esp_err_t synapse_acquisition_start_session(const acq_session_config_t *config) {
    if (!config) return ESP_ERR_INVALID_ARG;
    if (g_acq.session_active) {
        ESP_LOGW(TAG, "Session already active");
        return ESP_ERR_INVALID_STATE;
    }
    if (g_acq.current_tier == ACQ_TIER_2) {
        ESP_LOGW(TAG, "Already in Tier 2");
        return ESP_ERR_INVALID_STATE;
    }
    
    // Check power budget for Tier 2
    if (!g_acq.power_t2_affordable) {
        ESP_LOGW(TAG, "Power budget insufficient for Tier 2 session");
        return ESP_ERR_NOT_ALLOWED;
    }
    
    return start_session_internal(config);
}

esp_err_t synapse_acquisition_stop_session(void) {
    if (!g_acq.session_active) return ESP_OK;
    return stop_session_internal();
}

esp_err_t synapse_acquisition_get_session_status(bool *active, acq_tier_t *tier, uint32_t *elapsed_sec) {
    if (active) *active = g_acq.session_active;
    if (tier) *tier = g_acq.current_tier;
    if (elapsed_sec && g_acq.session_active) {
        *elapsed_sec = (esp_timer_get_time() - g_acq.session.start_time_utc * 1000000LL) / 1000000;
    }
    return ESP_OK;
}

acq_tier_t synapse_acquisition_get_tier(void) {
    return g_acq.current_tier;
}

bool synapse_acquisition_is_motion_gate_armed(void) {
    return g_acq.motion_gate_armed;
}

void synapse_acquisition_update_motion_gate(float sqi, float map) {
    bool clean = (sqi >= g_acq.sqi_min) && (map <= g_acq.map_max);
    
    if (clean) {
        g_acq.consecutive_clean_windows++;
        if (g_acq.consecutive_clean_windows >= g_acq.required_consecutive_clean) {
            if (!g_acq.motion_gate_armed) {
                g_acq.motion_gate_armed = true;
                ESP_LOGI(TAG, "Motion gate ARMED (clean windows: %d)", g_acq.consecutive_clean_windows);
                if (g_acq.system_events) {
                    xEventGroupSetBits(g_acq.system_events, ACQ_EVT_MOTION_GATE);
                }
            }
        }
    } else {
        if (g_acq.consecutive_clean_windows > 0) {
            g_acq.consecutive_clean_windows = 0;
            if (g_acq.motion_gate_armed) {
                g_acq.motion_gate_armed = false;
                ESP_LOGI(TAG, "Motion gate DISARMED (sqi=%.3f, map=%.3f)", sqi, map);
                if (g_acq.system_events) {
                    xEventGroupClearBits(g_acq.system_events, ACQ_EVT_MOTION_GATE);
                }
            }
        }
    }
}

void synapse_acquisition_update_immobility(float motion_intensity, bool is_stationary) {
    if (is_stationary && motion_intensity < g_acq.accel_threshold_g) {
        g_acq.immobility_counter++;
        g_acq.last_motion_time_us = esp_timer_get_time();
    } else {
        g_acq.immobility_counter = 0;
    }
    
    // Signal immobility event if threshold reached
    if (g_acq.immobility_counter >= g_acq.required_immobility_windows) {
        if (g_acq.system_events) {
            xEventGroupSetBits(g_acq.system_events, ACQ_EVT_IMMOBILITY);
        }
    }
}

void synapse_acquisition_update_power_budget(bool t1_affordable, bool t2_affordable,
                                              float tier1_h_used, float tier2_min_used) {
    g_acq.power_t1_affordable = t1_affordable;
    g_acq.power_t2_affordable = t2_affordable;
    g_acq.tier1_h_used_today = tier1_h_used;
    g_acq.tier2_min_used_today = tier2_min_used;
}

esp_err_t synapse_acquisition_set_tier(acq_tier_t tier) {
    if (tier > ACQ_TIER_2) return ESP_ERR_INVALID_ARG;
    g_acq.target_tier = tier;
    return ESP_OK;
}

void synapse_acquisition_set_session_callback(void (*callback)(const acq_session_t *session, void *user_data), void *user_data) {
    g_session_callback = callback;
    g_session_user_data = user_data;
}

// ============================================================================
// Internal FSM Implementation
// ============================================================================

static void fsm_task_fn(void *arg) {
    (void)arg;
    TickType_t last_wake = xTaskGetTickCount();
    TickType_t period_ticks = pdMS_TO_TICKS(ACQ_FSM_PERIOD_MS);

    ESP_LOGI(TAG, "FSM task started (1Hz)");

    while (g_acq.running) {
        vTaskDelayUntil(&last_wake, period_ticks);

        // Update night window
        update_night_window();

        // Check tier transitions
        check_tier_promotion();
        check_tier_demotion();

        // Execute current tier logic
        switch (g_acq.current_tier) {
            case ACQ_TIER_0:
                handle_tier_0();
                break;
            case ACQ_TIER_1:
                handle_tier_1();
                break;
            case ACQ_TIER_2:
                handle_tier_2();
                break;
        }

        // Update session elapsed time
        if (g_acq.session_active) {
            g_acq.session.elapsed_sec = (esp_timer_get_time() - g_acq.session.start_time_utc * 1000000LL) / 1000000;
            
            // Check session duration
            if (g_acq.session.config.duration_sec > 0 &&
                g_acq.session.elapsed_sec >= g_acq.session.config.duration_sec) {
                ESP_LOGI(TAG, "Session duration reached, stopping");
                stop_session_internal();
            }
        }

        // Update auxiliary sensor data (GPS, Temp)
        synapse_sensors_update_aux_data();
    }

    vTaskDelete(NULL);
}

static esp_err_t transition_to_tier(acq_tier_t new_tier) {
    if (new_tier == g_acq.current_tier) return ESP_OK;
    
    acq_tier_t old_tier = g_acq.current_tier;
    g_acq.current_tier = new_tier;
    g_acq.tier_transitions[new_tier]++;
    
    ESP_LOGI(TAG, "Tier transition: %d -> %d", old_tier, new_tier);
    
    // Update event bits
    if (g_acq.system_events) {
        // Clear old tier bits
        xEventGroupClearBits(g_acq.system_events, ACQ_EVT_T0_ACTIVE | ACQ_EVT_T1_ACTIVE | ACQ_EVT_T2_ACTIVE);
        // Set new tier bit
        switch (new_tier) {
            case ACQ_TIER_0: xEventGroupSetBits(g_acq.system_events, ACQ_EVT_T0_ACTIVE); break;
            case ACQ_TIER_1: xEventGroupSetBits(g_acq.system_events, ACQ_EVT_T1_ACTIVE); break;
            case ACQ_TIER_2: xEventGroupSetBits(g_acq.system_events, ACQ_EVT_T2_ACTIVE); break;
        }
    }
    
    // Notify power monitor of tier change
    synapse_power_on_tier_change(old_tier, new_tier);
    
    return ESP_OK;
}

static void handle_tier_0(void) {
    // Tier 0: Continuous monitoring (always active)
    // Sensors already running at base rates via sensor_scheduler
    // ECG: 500Hz, PPG: 50/64Hz, IMU: 100Hz, GPS: 1Hz, Temp: 1Hz
    
    // Ensure sensors are running
    if (!synapse_sensors_is_running()) {
        synapse_sensors_start();
    }
}

static void handle_tier_1(void) {
    // Tier 1: Enhanced monitoring during sleep/rest
    // For Band v1: Increase sampling rates, enable additional processing
    
    // Could increase IMU rate for sleep staging, enable more detailed PPG analysis
    // For now, just ensure sensors running
    if (!synapse_sensors_is_running()) {
        synapse_sensors_start();
    }
}

static void handle_tier_2(void) {
    // Tier 2: Active sport session
    // Session recording active, FIT writing enabled
    
    if (!g_acq.session_active) {
        // Session should have been started before entering Tier 2
        ESP_LOGW(TAG, "Tier 2 active but no session - demoting");
        transition_to_tier(ACQ_TIER_0);
    }
}

static void update_night_window(void) {
    time_t now = get_utc_time();
    struct tm *tm = gmtime(&now);
    
    bool in_night = false;
    if (g_acq.sleep_start_hour > g_acq.sleep_end_hour) {
        // Overnight window (e.g., 23-7)
        in_night = (tm->tm_hour >= g_acq.sleep_start_hour) || (tm->tm_hour < g_acq.sleep_end_hour);
    } else {
        // Same-day window
        in_night = (tm->tm_hour >= g_acq.sleep_start_hour) && (tm->tm_hour < g_acq.sleep_end_hour);
    }
    
    if (in_night != g_acq.night_window_active) {
        g_acq.night_window_active = in_night;
        ESP_LOGI(TAG, "Night window: %s", in_night ? "ACTIVE" : "INACTIVE");
        
        if (g_acq.system_events) {
            if (in_night) {
                xEventGroupSetBits(g_acq.system_events, ACQ_EVT_NIGHT_WINDOW);
            } else {
                xEventGroupClearBits(g_acq.system_events, ACQ_EVT_NIGHT_WINDOW);
            }
        }
    }
}

static void check_tier_promotion(void) {
    // T0 -> T1 promotion conditions:
    // 1. Night window active AND motion gate armed
    // 2. Sustained immobility (>5 min) AND motion gate armed
    // 3. Manual trigger (target_tier set)
    
    if (g_acq.current_tier == ACQ_TIER_0) {
        bool promote = false;
        
        if (g_acq.target_tier > ACQ_TIER_0) {
            promote = true;
            ESP_LOGI(TAG, "Manual tier promotion requested");
        } else if (g_acq.night_window_active && g_acq.motion_gate_armed && g_acq.power_t1_affordable) {
            promote = true;
            ESP_LOGI(TAG, "Auto-promotion: Night window + motion gate armed");
        } else if (g_acq.immobility_counter >= g_acq.required_immobility_windows && 
                   g_acq.motion_gate_armed && g_acq.power_t1_affordable) {
            promote = true;
            ESP_LOGI(TAG, "Auto-promotion: Sustained immobility + motion gate armed");
        }
        
        if (promote) {
            transition_to_tier(ACQ_TIER_1);
        }
    }
    // T1 -> T2 promotion (manual session start)
    else if (g_acq.current_tier == ACQ_TIER_1 && g_acq.target_tier == ACQ_TIER_2) {
        if (g_acq.power_t2_affordable) {
            transition_to_tier(ACQ_TIER_2);
        } else {
            ESP_LOGW(TAG, "Cannot promote to T2: power budget insufficient");
            g_acq.target_tier = ACQ_TIER_1;
        }
    }
}

static void check_tier_demotion(void) {
    // T1 -> T0 demotion:
    // 1. Night window ended
    // 2. Motion detected (motion gate disarmed)
    // 3. Power budget exhausted
    // 4. Manual target_tier change
    
    if (g_acq.current_tier == ACQ_TIER_1) {
        bool demote = false;
        
        if (g_acq.target_tier == ACQ_TIER_0) {
            demote = true;
            ESP_LOGI(TAG, "Manual tier demotion requested");
        } else if (!g_acq.night_window_active && g_acq.immobility_counter == 0) {
            demote = true;
            ESP_LOGI(TAG, "Auto-demotion: Night window ended, motion detected");
        } else if (!g_acq.motion_gate_armed) {
            demote = true;
            ESP_LOGI(TAG, "Auto-demotion: Motion gate disarmed");
        } else if (!g_acq.power_t1_affordable) {
            demote = true;
            ESP_LOGW(TAG, "Auto-demotion: Tier 1 power budget exhausted");
        }
        
        if (demote) {
            transition_to_tier(ACQ_TIER_0);
        }
    }
    // T2 -> T1/T0 demotion (session ended)
    else if (g_acq.current_tier == ACQ_TIER_2) {
        if (!g_acq.session_active) {
            // Session ended, return to previous tier
            acq_tier_t target = g_acq.night_window_active ? ACQ_TIER_1 : ACQ_TIER_0;
            transition_to_tier(target);
        }
    }
}

static esp_err_t start_session_internal(const acq_session_config_t *config) {
    memcpy(&g_acq.session.config, config, sizeof(acq_session_config_t));
    g_acq.session.active = true;
    g_acq.session.current_tier = ACQ_TIER_2;
    g_acq.session.record_count = 0;
    g_acq.session.start_time_utc = get_utc_time();
    g_acq.session.elapsed_sec = 0;
    
    // Generate FIT file path
    snprintf(g_acq.session.filepath, sizeof(g_acq.session.filepath), 
             "/storage/session_%08x.fit", (unsigned)g_acq.session.start_time_utc);
    
    // Start FIT session
    fit_session_config_t fit_cfg = {
        .sport_type = (fit_sport_t)config->sport_type,
        .start_time_utc = g_acq.session.start_time_utc,
        .duration_sec = config->duration_sec,
        .has_gps = config->enable_gps,
    };
    ESP_ERROR_CHECK(synapse_fit_start_session(&fit_cfg));
    
    g_acq.session_active = true;
    
    // Signal session start
    if (g_acq.system_events) {
        xEventGroupSetBits(g_acq.system_events, ACQ_EVT_SESSION_START);
    }
    
    // Transition to Tier 2
    transition_to_tier(ACQ_TIER_2);
    
    ESP_LOGI(TAG, "Session started: sport=%d, duration=%" PRIu32 "s, file=%s",
             config->sport_type, config->duration_sec, g_acq.session.filepath);
    return ESP_OK;
}

static esp_err_t stop_session_internal(void) {
    if (!g_acq.session_active) return ESP_OK;
    
    // Stop FIT session
    ESP_ERROR_CHECK(synapse_fit_stop_session(NULL));
    
    // Call session callback
    if (g_session_callback) {
        g_session_callback(&g_acq.session, g_session_user_data);
    }
    
    g_acq.session_active = false;
    g_acq.sessions_completed++;
    
    // Signal session stop
    if (g_acq.system_events) {
        xEventGroupSetBits(g_acq.system_events, ACQ_EVT_SESSION_STOP);
    }
    
    ESP_LOGI(TAG, "Session stopped: records=%" PRIu32 ", elapsed=%" PRIu32 "s",
             g_acq.session.record_count, g_acq.session.elapsed_sec);
    
    // Target tier will be handled by check_tier_demotion
    g_acq.target_tier = g_acq.night_window_active ? ACQ_TIER_1 : ACQ_TIER_0;
    
    return ESP_OK;
}

static time_t get_utc_time(void) {
    // Use ESP32 time (assumes SNTP synced via provisioning)
    time_t now;
    time(&now);
    return now;
}