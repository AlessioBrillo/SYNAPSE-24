#include "unity.h"
#include "imu_processor.h"
#include <math.h>

// Synthetic IMU generation for testing

static void generate_static_imu(int sample_idx, float* ax, float* ay, float* az,
                                float* gx, float* gy, float* gz, float tilt_x, float tilt_y) {
    // Static orientation with gravity vector rotated by tilt angles
    float tx = tilt_x * M_PI / 180.0f;
    float ty = tilt_y * M_PI / 180.0f;
    
    // Gravity in sensor frame (1g = 9.81 m/s^2, but we use g units)
    *ax = sinf(tx);
    *ay = -sinf(ty) * cosf(tx);
    *az = cosf(ty) * cosf(tx);
    
    // Small noise on accelerometer
    float noise = ((float)(sample_idx * 1664525 + 1013904223) % 2147483647) / 2147483647.0f * 2.0f - 1.0f;
    *ax += noise * 0.01f;
    *ay += noise * 0.01f;
    *az += noise * 0.01f;
    
    // Gyro near zero (static)
    *gx = ((float)(sample_idx * 48271 + 37459) % 2147483647) / 2147483647.0f * 2.0f - 1.0f;
    *gy = ((float)(sample_idx * 9301 + 49297) % 2147483647) / 2147483647.0f * 2.0f - 1.0f;
    *gz = ((float)(sample_idx * 23902 + 1103515245) % 2147483647) / 2147483647.0f * 2.0f - 1.0f;
    *gx *= 0.001f;  // 0.001 rad/s noise
    *gy *= 0.001f;
    *gz *= 0.001f;
}

static void generate_walking_imu(int sample_idx, float* ax, float* ay, float* az,
                                 float* gx, float* gy, float* gz) {
    // Walking pattern: ~2 Hz vertical oscillation, ~1 Hz anterior-posterior
    float t = (float)sample_idx / 100.0f;
    
    // Vertical acceleration (strongest)
    *az = 1.0f + 0.5f * sinf(2.0f * M_PI * 2.0f * t) + 0.2f * sinf(2.0f * M_PI * 4.0f * t);
    
    // Anterior-posterior
    *ax = 0.3f * sinf(2.0f * M_PI * 1.0f * t + M_PI/4);
    
    // Medial-lateral
    *ay = 0.1f * sinf(2.0f * M_PI * 1.0f * t);
    
    // Gyro: arm swing / body rotation
    *gx = 0.2f * sinf(2.0f * M_PI * 1.0f * t);
    *gy = 0.1f * sinf(2.0f * M_PI * 2.0f * t);
    *gz = 0.05f * sinf(2.0f * M_PI * 1.0f * t);
    
    // Add noise
    float noise = ((float)(sample_idx * 1664525 + 1013904223) % 2147483647) / 2147483647.0f * 2.0f - 1.0f;
    *ax += noise * 0.02f;
    *ay += noise * 0.02f;
    *az += noise * 0.02f;
}

static void generate_running_imu(int sample_idx, float* ax, float* ay, float* az,
                                 float* gx, float* gy, float* gz) {
    // Running: higher frequency, higher amplitude
    float t = (float)sample_idx / 100.0f;
    
    *az = 1.0f + 1.2f * sinf(2.0f * M_PI * 3.0f * t);
    *ax = 0.8f * sinf(2.0f * M_PI * 2.5f * t);
    *ay = 0.3f * sinf(2.0f * M_PI * 2.5f * t + M_PI/3);
    
    *gx = 0.5f * sinf(2.0f * M_PI * 2.5f * t);
    *gy = 0.3f * sinf(2.0f * M_PI * 2.5f * t);
    *gz = 0.2f * sinf(2.0f * M_PI * 2.5f * t);
    
    float noise = ((float)(sample_idx * 1664525 + 1013904223) % 2147483647) / 2147483647.0f * 2.0f - 1.0f;
    *ax += noise * 0.05f;
    *ay += noise * 0.05f;
    *az += noise * 0.05f;
}

void setUp(void) {
    imu_processor_reset();
}

void tearDown(void) {
    imu_processor_reset();
}

void test_imu_processor_static_posture(void) {
    imu_features_t features = {};
    
    // 10 seconds of static data at 100Hz
    for (int i = 0; i < 10 * 100; i++) {
        float ax, ay, az, gx, gy, gz;
        generate_static_imu(i, &ax, &ay, &az, &gx, &gy, &gz, 10.0f, -5.0f);
        int64_t ts = i * 10000;
        imu_processor_process_sample(ax, ay, az, gx, gy, gz, ts, &features);
    }
    
    TEST_ASSERT_TRUE(features.motion_intensity < 0.05f);  // Very low motion
    TEST_ASSERT_TRUE(features.is_stationary);
    TEST_ASSERT_FLOAT_WITHIN(2.0f, 10.0f, features.tilt_x_deg);
    TEST_ASSERT_FLOAT_WITHIN(2.0f, -5.0f, features.tilt_y_deg);
    TEST_ASSERT_GREATER_THAN(0.5f, features.sleep_probability);  // Static -> high sleep prob
}

void test_imu_processor_walking_motion(void) {
    imu_features_t features = {};
    
    for (int i = 0; i < 10 * 100; i++) {
        float ax, ay, az, gx, gy, gz;
        generate_walking_imu(i, &ax, &ay, &az, &gx, &gy, &gz);
        int64_t ts = i * 10000;
        imu_processor_process_sample(ax, ay, az, gx, gy, gz, ts, &features);
    }
    
    TEST_ASSERT_TRUE(features.motion_intensity > 0.2f);  // Significant motion
    TEST_ASSERT_FALSE(features.is_stationary);
    TEST_ASSERT_GREATER_THAN(1.5f, features.dominant_freq_hz);  // Walking ~2Hz
    TEST_ASSERT_LESS_THAN(3.0f, features.dominant_freq_hz);
    TEST_ASSERT_LESS_THAN(0.5f, features.sleep_probability);  // Walking -> low sleep prob
}

void test_imu_processor_running_motion(void) {
    imu_features_t features = {};
    
    for (int i = 0; i < 10 * 100; i++) {
        float ax, ay, az, gx, gy, gz;
        generate_running_imu(i, &ax, &ay, &az, &gx, &gy, &gz);
        int64_t ts = i * 10000;
        imu_processor_process_sample(ax, ay, az, gx, gy, gz, ts, &features);
    }
    
    TEST_ASSERT_TRUE(features.motion_intensity > 0.5f);  // High motion
    TEST_ASSERT_FALSE(features.is_stationary);
    TEST_ASSERT_GREATER_THAN(2.5f, features.dominant_freq_hz);  // Running ~3Hz
    TEST_ASSERT_LESS_THAN(0.3f, features.sleep_probability);  // Running -> very low sleep prob
}

void test_imu_processor_spectral_entropy(void) {
    imu_features_t features = {};
    
    // Static: low entropy (periodic)
    for (int i = 0; i < 10 * 100; i++) {
        float ax, ay, az, gx, gy, gz;
        generate_static_imu(i, &ax, &ay, &az, &gx, &gy, &gz, 0.0f, 0.0f);
        int64_t ts = i * 10000;
        imu_processor_process_sample(ax, ay, az, gx, gy, gz, ts, &features);
    }
    float static_entropy = features.spectral_entropy;
    
    imu_processor_reset();
    
    // Walking: higher entropy (more complex)
    for (int i = 0; i < 10 * 100; i++) {
        float ax, ay, az, gx, gy, gz;
        generate_walking_imu(i, &ax, &ay, &az, &gx, &gy, &gz);
        int64_t ts = i * 10000;
        imu_processor_process_sample(ax, ay, az, gx, gy, gz, ts, &features);
    }
    float walking_entropy = features.spectral_entropy;
    
    // Walking should have higher spectral entropy than static
    TEST_ASSERT_GREATER_THAN(static_entropy, walking_entropy);
}

void test_imu_processor_output_rate(void) {
    imu_features_t features = {};
    int output_count = 0;
    
    // 20 seconds of data
    for (int i = 0; i < 20 * 100; i++) {
        float ax, ay, az, gx, gy, gz;
        generate_static_imu(i, &ax, &ay, &az, &gx, &gy, &gz, 0.0f, 0.0f);
        int64_t ts = i * 10000;
        esp_err_t ret = imu_processor_process_sample(ax, ay, az, gx, gy, gz, ts, &features);
        if (ret == ESP_OK) output_count++;
    }
    
    // 20 seconds at 1Hz = ~20 outputs (after 10s warmup ~10 outputs)
    TEST_ASSERT_GREATER_OR_EQUAL(8, output_count);
    TEST_ASSERT_LESS_OR_EQUAL(22, output_count);
}

void test_imu_processor_rms_computation(void) {
    imu_features_t features = {};
    
    // Known signal: constant 0.5g on X axis
    for (int i = 0; i < 10 * 100; i++) {
        float ax = 0.5f, ay = 0.0f, az = 1.0f;  // 0.5g + 1g gravity
        float gx = 0.0f, gy = 0.0f, gz = 0.0f;
        int64_t ts = i * 10000;
        imu_processor_process_sample(ax, ay, az, gx, gy, gz, ts, &features);
    }
    
    // RMS of X should be ~0.5g (0.5g AC + small DC from gravity component)
    // Total motion intensity = sqrt(ax^2 + ay^2 + az^2) ≈ sqrt(0.25 + 0 + 1) = 1.12g
    TEST_ASSERT_FLOAT_WITHIN(0.1f, 0.5f, features.acc_rms_x);
    TEST_ASSERT_FLOAT_WITHIN(0.1f, 0.0f, features.acc_rms_y);
    TEST_ASSERT_FLOAT_WITHIN(0.1f, 1.0f, features.acc_rms_z);  // Mostly gravity
    TEST_ASSERT_FLOAT_WITHIN(0.15f, 1.12f, features.motion_intensity);
}

void test_imu_processor_gyro_rms(void) {
    imu_features_t features = {};
    
    // Known rotation: 0.1 rad/s on X axis
    for (int i = 0; i < 10 * 100; i++) {
        float ax = 0.0f, ay = 0.0f, az = 1.0f;
        float gx = 0.1f, gy = 0.0f, gz = 0.0f;
        int64_t ts = i * 10000;
        imu_processor_process_sample(ax, ay, az, gx, gy, gz, ts, &features);
    }
    
    TEST_ASSERT_FLOAT_WITHIN(0.01f, 0.1f, features.gx_rms);
    TEST_ASSERT_FLOAT_WITHIN(0.01f, 0.0f, features.gy_rms);
    TEST_ASSERT_FLOAT_WITHIN(0.01f, 0.0f, features.gz_rms);
}

void test_imu_sleep_wake_classifier(void) {
    // Test the classifier directly with known feature values
    imu_features_t sleep_features = {};
    sleep_features.motion_intensity = 0.01f;
    sleep_features.spectral_entropy = 0.1f;
    sleep_features.dominant_freq_hz = 0.2f;
    sleep_features.tilt_z_deg = 0.5f;
    
    float sleep_prob = imu_sleep_wake_classify(&sleep_features);
    TEST_ASSERT_GREATER_THAN(0.7f, sleep_prob);
    
    imu_features_t wake_features = {};
    wake_features.motion_intensity = 0.5f;
    wake_features.spectral_entropy = 0.8f;
    wake_features.dominant_freq_hz = 2.0f;
    wake_features.tilt_z_deg = 5.0f;
    
    float wake_prob = imu_sleep_wake_classify(&wake_features);
    TEST_ASSERT_LESS_THAN(0.3f, wake_prob);
    
    imu_features_t ambiguous_features = {};
    ambiguous_features.motion_intensity = 0.1f;
    ambiguous_features.spectral_entropy = 0.4f;
    ambiguous_features.dominant_freq_hz = 1.0f;
    ambiguous_features.tilt_z_deg = 2.0f;
    
    float ambig_prob = imu_sleep_wake_classify(&ambiguous_features);
    TEST_ASSERT_GREATER_THAN(0.2f, ambig_prob);
    TEST_ASSERT_LESS_THAN(0.8f, ambig_prob);
}

void test_imu_processor_insufficient_data(void) {
    imu_features_t features = {};
    
    // Only 1 second of data
    for (int i = 0; i < 100; i++) {
        float ax, ay, az, gx, gy, gz;
        generate_static_imu(i, &ax, &ay, &az, &gx, &gy, &gz, 0.0f, 0.0f);
        int64_t ts = i * 10000;
        esp_err_t ret = imu_processor_process_sample(ax, ay, az, gx, gy, gz, ts, &features);
        if (i < 90) {
            TEST_ASSERT_EQUAL(ESP_ERR_NOT_FINISHED, ret);
        }
    }
}

void test_imu_processor_reset(void) {
    imu_features_t features = {};
    
    // Fill buffer
    for (int i = 0; i < 10 * 100; i++) {
        float ax, ay, az, gx, gy, gz;
        generate_static_imu(i, &ax, &ay, &az, &gx, &gy, &gz, 0.0f, 0.0f);
        int64_t ts = i * 10000;
        imu_processor_process_sample(ax, ay, az, gx, gy, gz, ts, &features);
    }
    
    TEST_ASSERT_TRUE(features.motion_intensity < 0.05f);
    
    // Reset
    imu_processor_reset();
    
    imu_features_t features2 = {};
    imu_processor_get_latest(&features2);
    // After reset, motion_intensity should be 0 (uninitialized)
    TEST_ASSERT_EQUAL_FLOAT(0.0f, features2.motion_intensity);
}

int main(void) {
    UNITY_BEGIN();
    
    RUN_TEST(test_imu_processor_static_posture);
    RUN_TEST(test_imu_processor_walking_motion);
    RUN_TEST(test_imu_processor_running_motion);
    RUN_TEST(test_imu_processor_spectral_entropy);
    RUN_TEST(test_imu_processor_output_rate);
    RUN_TEST(test_imu_processor_rms_computation);
    RUN_TEST(test_imu_processor_gyro_rms);
    RUN_TEST(test_imu_sleep_wake_classifier);
    RUN_TEST(test_imu_processor_insufficient_data);
    RUN_TEST(test_imu_processor_reset);
    
    return UNITY_END();
}