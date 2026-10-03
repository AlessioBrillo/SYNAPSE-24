/**
 * @file synapse_acquisition.h
 * @brief Synapse Band v1 - Tiered Acquisition FSM (T0/T1/T2)
 * 
 * Implements the tiered acquisition state machine per Architecture.md §33-43:
 * - Tier 0 (Continuous H24): PPG, IMU, Temp, 1-2ch EEG (always on)
 * - Tier 1 (Rest/Sleep): Multi-channel EEG, fNIRS, ECG (triggered by immobility/night)
 * - Tier 2 (Voluntary): On-demand ECG, cognitive tests, calibration
 * 
 * For Band v1 (sports wearable): T0 = ECG+PPG+IMU+GPS+Temp continuous
 * T1 = Enhanced sampling during sleep/rest (triggered by IMU immobility)
 * T2 = Manual session start (sport recording)
 */

#ifndef SYNAPSE_ACQUISITION_H
#define SYNAPSE_ACQUISITION_H

#include <stdint.h>
#include <stdbool.h>
#include "esp_err.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"

#ifdef __cplusplus
extern "C" {
#endif

// Event bits for acquisition FSM (shared with main)
#define ACQ_EVT_T0_ACTIVE       BIT0
#define ACQ_EVT_T1_ACTIVE       BIT1
#define ACQ_EVT_T2_ACTIVE       BIT2
#define ACQ_EVT_SESSION_START   BIT3
#define ACQ_EVT_SESSION_STOP    BIT4
#define ACQ_EVT_MOTION_GATE     BIT5
#define ACQ_EVT_NIGHT_WINDOW    BIT6
#define ACQ_EVT_IMMOBILITY      BIT7
#define ACQ_EVT_POWER_LOW       BIT8

// Acquisition tiers
typedef enum {
    ACQ_TIER_0 = 0,  // Continuous monitoring (always on)
    ACQ_TIER_1 = 1,  // High-density rest/sleep
    ACQ_TIER_2 = 2,  // Voluntary/active sessions
} acq_tier_t;

// Sport types (matches FIT profile)
typedef enum {
    ACQ_SPORT_GENERIC = 0,
    ACQ_SPORT_RUNNING = 1,
    ACQ_SPORT_CYCLING = 2,
    ACQ_SPORT_SWIMMING = 3,
    ACQ_SPORT_WALKING = 4,
    ACQ_SPORT_HIKING = 5,
    ACQ_SPORT_SLEEP = 6,
} acq_sport_t;

// Session configuration
typedef struct {
    acq_sport_t sport_type;
    uint32_t duration_sec;         // 0 = infinite
    bool enable_gps;
    bool enable_ecg;
    uint16_t ecg_sample_rate;      // Hz
    bool enable_ppg;
    uint16_t ppg_sample_rate;      // Hz
    bool enable_imu;
    uint16_t imu_sample_rate;      // Hz
    bool enable_temp;
    bool auto_pause;               // Motion-based pause
} acq_session_config_t;

// Session state
typedef struct {
    bool active;
    acq_session_config_t config;
    char filepath[64];
    uint32_t record_count;
    uint32_t start_time_utc;
    uint32_t elapsed_sec;
    acq_tier_t current_tier;
} acq_session_t;

// Acquisition FSM state
typedef struct {
    // Configuration
    acq_tier_t current_tier;
    acq_tier_t target_tier;
    
    // Session
    acq_session_t session;
    bool session_active;
    
    // Motion gate (Architecture.md §74)
    bool motion_gate_armed;
    uint8_t consecutive_clean_windows;
    float sqi_min;
    float map_max;
    uint8_t required_consecutive_clean;
    
    // Immobility detection (for T0->T1 promotion)
    uint32_t immobility_window_s;
    float accel_threshold_g;
    uint8_t required_immobility_windows;
    uint8_t immobility_counter;
    int64_t last_motion_time_us;
    
    // Night window scheduler
    uint8_t sleep_start_hour;
    uint8_t sleep_end_hour;
    bool night_window_active;
    
    // Power budget awareness
    bool power_t1_affordable;
    bool power_t2_affordable;
    float tier1_h_used_today;
    float tier2_min_used_today;
    
    // Events
    EventGroupHandle_t system_events;
    
    // Task
    TaskHandle_t fsm_task;
    bool running;
    
    // Stats
    uint32_t tier_transitions[3];
    uint32_t sessions_completed;
} synapse_acquisition_t;


// ============================================================================
// Public API
// ============================================================================

/**
 * @brief Initialize acquisition FSM
 * @param system_events System event group for cross-module signaling
 * @return ESP_OK on success
 */
esp_err_t synapse_acquisition_init(EventGroupHandle_t system_events);

/**
 * @brief Start acquisition FSM (creates FreeRTOS task)
 * @return ESP_OK on success
 */
esp_err_t synapse_acquisition_start(void);

/**
 * @brief Stop acquisition FSM
 * @return ESP_OK on success
 */
esp_err_t synapse_acquisition_stop(void);

/**
 * @brief Deinitialize acquisition FSM
 * @return ESP_OK on success
 */
esp_err_t synapse_acquisition_deinit(void);

/**
 * @brief Periodic tick (call from main loop or dedicated task)
 * Handles tier transitions, motion gate, immobility, night window
 */
void synapse_acquisition_tick(void);

/**
 * @brief Start a recording session (Tier 2)
 * @param config Session configuration
 * @return ESP_OK on success
 */
esp_err_t synapse_acquisition_start_session(const acq_session_config_t *config);

/**
 * @brief Stop current recording session
 * @return ESP_OK on success
 */
esp_err_t synapse_acquisition_stop_session(void);

/**
 * @brief Get current session status
 * @param[out] active Session active flag
 * @param[out] tier Current tier
 * @param[out] elapsed_sec Session elapsed time
 * @return ESP_OK on success
 */
esp_err_t synapse_acquisition_get_session_status(bool *active, acq_tier_t *tier, uint32_t *elapsed_sec);

/**
 * @brief Get current acquisition tier
 * @return Current tier
 */
acq_tier_t synapse_acquisition_get_tier(void);

/**
 * @brief Check if motion gate is armed (Tier 1 promotion allowed)
 * @return true if armed
 */
bool synapse_acquisition_is_motion_gate_armed(void);

/**
 * @brief Update motion gate state from signal quality module
 * @param sqi Signal Quality Index (0.0-1.0)
 * @param map Motion Artifact Probability (0.0-1.0)
 */
void synapse_acquisition_update_motion_gate(float sqi, float map);

/**
 * @brief Update immobility state from IMU features
 * @param motion_intensity Current motion intensity (g)
 * @param is_stationary True if IMU detects stationary state
 */
void synapse_acquisition_update_immobility(float motion_intensity, bool is_stationary);

/**
 * @brief Update power budget affordability
 * @param t1_affordable Can afford Tier 1
 * @param t2_affordable Can afford Tier 2
 * @param tier1_h_used Tier 1 hours used today
 * @param tier2_min_used Tier 2 minutes used today
 */
void synapse_acquisition_update_power_budget(bool t1_affordable, bool t2_affordable,
                                              float tier1_h_used, float tier2_min_used);

/**
 * @brief Force tier transition (for testing/manual override)
 * @param tier Target tier
 * @return ESP_OK on success
 */
esp_err_t synapse_acquisition_set_tier(acq_tier_t tier);

/**
 * @brief Register session data callback
 * Called when session record is ready for FIT/BLE
 * @param callback Callback function
 * @param user_data User data pointer
 */
void synapse_acquisition_set_session_callback(void (*callback)(const acq_session_t *session, void *user_data), void *user_data);

#ifdef __cplusplus
}
#endif

#endif // SYNAPSE_ACQUISITION_H