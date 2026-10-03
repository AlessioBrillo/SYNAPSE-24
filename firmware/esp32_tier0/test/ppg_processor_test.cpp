#include "unity.h"
#include "ppg_processor.h"
#include "ppg_sqi.h"
#include <math.h>

// Synthetic PPG generation (adapted from NeuroKit2 ecg_simulate principles)
// Generates clean PPG-like signal at 64Hz with known heart rate

static float generate_ppg_sample(int sample_idx, float hr_bpm, float fs_hz, float noise_level) {
    float t = (float)sample_idx / fs_hz;
    float period = 60.0f / hr_bpm;
    float phase = 2.0f * M_PI * t / period;
    
    // PPG-like waveform: systolic upstroke + dicrotic notch + diastolic decay
    float signal = 0.0f;
    signal += 1.0f * sinf(phase);                    // Fundamental
    signal += 0.3f * sinf(2.0f * phase);             // 2nd harmonic
    signal += 0.15f * sinf(3.0f * phase);            // 3rd harmonic
    signal += 0.05f * sinf(4.0f * phase);            // 4th harmonic
    
    // Add baseline wander (respiratory)
    signal += 0.1f * sinf(2.0f * M_PI * t / 4.0f);   // 0.25 Hz respiration
    
    // Add noise
    if (noise_level > 0.0f) {
        // Simple pseudo-random noise
        float noise = ((float)(sample_idx * 1664525 + 1013904223) % 2147483647) / 2147483647.0f * 2.0f - 1.0f;
        signal += noise_level * noise;
    }
    
    // Scale to realistic PPG range (IR channel ~10000-50000 counts)
    return 20000.0f + signal * 5000.0f;
}

static ppg_sqi_result_t make_good_sqi(void) {
    ppg_sqi_result_t sqi = {};
    sqi.sqi = 0.8f;
    sqi.perfusion_index = 2.5f;
    sqi.motion_artifact_prob = 0.1f;
    sqi.timestamp_us = 0;
    return sqi;
}

static ppg_sqi_result_t make_bad_sqi(void) {
    ppg_sqi_result_t sqi = {};
    sqi.sqi = 0.2f;
    sqi.perfusion_index = 0.5f;
    sqi.motion_artifact_prob = 0.8f;
    sqi.timestamp_us = 0;
    return sqi;
}

void setUp(void) {
    ppg_processor_reset();
    ppg_sqi_init();
}

void tearDown(void) {
    ppg_processor_reset();
}

void test_ppg_processor_hr_accuracy_60bpm(void) {
    // Generate 5 seconds of 60 BPM PPG at 64Hz
    const int samples = 5 * 64;
    ppg_features_t features = {};
    ppg_sqi_result_t sqi = make_good_sqi();
    int computed_count = 0;
    
    for (int i = 0; i < samples; i++) {
        float ir = generate_ppg_sample(i, 60.0f, 64.0f, 0.05f);
        float red = ir * 0.7f;  // RED typically lower amplitude
        int64_t ts = i * (1000000 / 64);
        
        esp_err_t ret = ppg_processor_process_sample(red, ir, ts, &sqi, &features);
        if (ret == ESP_OK) {
            computed_count++;
        }
    }
    
    // Should have computed features at ~10Hz (50 times in 5 seconds)
    TEST_ASSERT_GREATER_OR_EQUAL(40, computed_count);
    TEST_ASSERT_LESS_OR_EQUAL(60, computed_count);
    
    // Final features should be valid with accurate HR
    TEST_ASSERT_TRUE(features.valid);
    TEST_ASSERT_FALSE(features.motion_artifact);
    TEST_ASSERT_FLOAT_WITHIN(2.0f, 60.0f, features.hr_bpm);  // MAE < 2 bpm
    TEST_ASSERT_GREATER_THAN(3, features.peak_count);
}

void test_ppg_processor_hr_accuracy_80bpm(void) {
    const int samples = 5 * 64;
    ppg_features_t features = {};
    ppg_sqi_result_t sqi = make_good_sqi();
    
    for (int i = 0; i < samples; i++) {
        float ir = generate_ppg_sample(i, 80.0f, 64.0f, 0.05f);
        float red = ir * 0.7f;
        int64_t ts = i * (1000000 / 64);
        ppg_processor_process_sample(red, ir, ts, &sqi, &features);
    }
    
    TEST_ASSERT_TRUE(features.valid);
    TEST_ASSERT_FLOAT_WITHIN(2.0f, 80.0f, features.hr_bpm);
}

void test_ppg_processor_hr_accuracy_120bpm(void) {
    const int samples = 5 * 64;
    ppg_features_t features = {};
    ppg_sqi_result_t sqi = make_good_sqi();
    
    for (int i = 0; i < samples; i++) {
        float ir = generate_ppg_sample(i, 120.0f, 64.0f, 0.05f);
        float red = ir * 0.7f;
        int64_t ts = i * (1000000 / 64);
        ppg_processor_process_sample(red, ir, ts, &sqi, &features);
    }
    
    TEST_ASSERT_TRUE(features.valid);
    TEST_ASSERT_FLOAT_WITHIN(2.0f, 120.0f, features.hr_bpm);
}

void test_ppg_processor_hrv_rmssd(void) {
    // Generate PPG with known RR interval variability
    // Simulate RMSSD ~30ms by adding controlled jitter
    const int samples = 5 * 64;
    ppg_features_t features = {};
    ppg_sqi_result_t sqi = make_good_sqi();
    
    // Use fixed seed for reproducible jitter
    float base_hr = 70.0f;
    float rr_variability_ms = 30.0f;
    
    for (int i = 0; i < samples; i++) {
        // Add slight HR variation
        float hr = base_hr + 5.0f * sinf(2.0f * M_PI * i / (64.0f * 10.0f));  // 0.1 Hz HRV
        float ir = generate_ppg_sample(i, hr, 64.0f, 0.03f);
        float red = ir * 0.7f;
        int64_t ts = i * (1000000 / 64);
        ppg_processor_process_sample(red, ir, ts, &sqi, &features);
    }
    
    TEST_ASSERT_TRUE(features.valid);
    TEST_ASSERT_FLOAT_WITHIN(2.0f, base_hr, features.hr_bpm);
    // RMSSD should be reasonable for this variability
    TEST_ASSERT_GREATER_THAN(10.0f, features.rmssd_ms);
    TEST_ASSERT_LESS_THAN(80.0f, features.rmssd_ms);
}

void test_ppg_processor_motion_artifact_flag(void) {
    const int samples = 5 * 64;
    ppg_features_t features = {};
    ppg_sqi_result_t bad_sqi = make_bad_sqi();
    
    for (int i = 0; i < samples; i++) {
        float ir = generate_ppg_sample(i, 70.0f, 64.0f, 0.3f);  // High noise
        float red = ir * 0.7f;
        int64_t ts = i * (1000000 / 64);
        ppg_processor_process_sample(red, ir, ts, &bad_sqi, &features);
    }
    
    // Should detect motion artifact from low SQI
    TEST_ASSERT_TRUE(features.motion_artifact);
    // Features may still be computed but flagged
}

void test_ppg_processor_insufficient_data(void) {
    ppg_features_t features = {};
    ppg_sqi_result_t sqi = make_good_sqi();
    
    // Only 1 second of data (insufficient for 5s window)
    for (int i = 0; i < 64; i++) {
        float ir = generate_ppg_sample(i, 70.0f, 64.0f, 0.05f);
        float red = ir * 0.7f;
        int64_t ts = i * (1000000 / 64);
        esp_err_t ret = ppg_processor_process_sample(red, ir, ts, &sqi, &features);
        
        if (i < 60) {
            TEST_ASSERT_EQUAL(ESP_ERR_NOT_FINISHED, ret);
        }
    }
    
    // Should not have valid features yet
    TEST_ASSERT_FALSE(features.valid);
}

void test_ppg_processor_snr_estimation(void) {
    const int samples = 5 * 64;
    ppg_features_t features = {};
    ppg_sqi_result_t sqi = make_good_sqi();
    
    // Clean signal
    for (int i = 0; i < samples; i++) {
        float ir = generate_ppg_sample(i, 70.0f, 64.0f, 0.01f);  // Very low noise
        float red = ir * 0.7f;
        int64_t ts = i * (1000000 / 64);
        ppg_processor_process_sample(red, ir, ts, &sqi, &features);
    }
    
    TEST_ASSERT_TRUE(features.valid);
    TEST_ASSERT_GREATER_THAN(20.0f, features.snr_db);  // Clean signal should have high SNR
    
    // Reset and test noisy signal
    ppg_processor_reset();
    
    for (int i = 0; i < samples; i++) {
        float ir = generate_ppg_sample(i, 70.0f, 64.0f, 0.5f);  // High noise
        float red = ir * 0.7f;
        int64_t ts = i * (1000000 / 64);
        ppg_processor_process_sample(red, ir, ts, &sqi, &features);
    }
    
    // Noisy signal should have lower SNR
    TEST_ASSERT_LESS_THAN(features.snr_db, 20.0f);
}

void test_ppg_processor_output_rate(void) {
    const int samples = 10 * 64;  // 10 seconds
    ppg_features_t features = {};
    ppg_sqi_result_t sqi = make_good_sqi();
    int output_count = 0;
    
    for (int i = 0; i < samples; i++) {
        float ir = generate_ppg_sample(i, 70.0f, 64.0f, 0.05f);
        float red = ir * 0.7f;
        int64_t ts = i * (1000000 / 64);
        esp_err_t ret = ppg_processor_process_sample(red, ir, ts, &sqi, &features);
        if (ret == ESP_OK) output_count++;
    }
    
    // 10 seconds at 10Hz = ~100 outputs (after initial 5s warmup ~50 outputs)
    TEST_ASSERT_GREATER_OR_EQUAL(40, output_count);
    TEST_ASSERT_LESS_OR_EQUAL(110, output_count);
}

void test_ppg_processor_reset(void) {
    const int samples = 5 * 64;
    ppg_features_t features = {};
    ppg_sqi_result_t sqi = make_good_sqi();
    
    // Fill buffer
    for (int i = 0; i < samples; i++) {
        float ir = generate_ppg_sample(i, 70.0f, 64.0f, 0.05f);
        float red = ir * 0.7f;
        int64_t ts = i * (1000000 / 64);
        ppg_processor_process_sample(red, ir, ts, &sqi, &features);
    }
    
    TEST_ASSERT_TRUE(features.valid);
    
    // Reset
    ppg_processor_reset();
    
    // Should need to refill
    ppg_features_t features2 = {};
    ppg_processor_get_latest(&features2);
    TEST_ASSERT_FALSE(features2.valid);  // Reset clears valid flag
}

int main(void) {
    UNITY_BEGIN();
    
    RUN_TEST(test_ppg_processor_hr_accuracy_60bpm);
    RUN_TEST(test_ppg_processor_hr_accuracy_80bpm);
    RUN_TEST(test_ppg_processor_hr_accuracy_120bpm);
    RUN_TEST(test_ppg_processor_hrv_rmssd);
    RUN_TEST(test_ppg_processor_motion_artifact_flag);
    RUN_TEST(test_ppg_processor_insufficient_data);
    RUN_TEST(test_ppg_processor_snr_estimation);
    RUN_TEST(test_ppg_processor_output_rate);
    RUN_TEST(test_ppg_processor_reset);
    
    return UNITY_END();
}