/**
 * @file synapse_night_window.h
 * @brief The wall-clock rule of the night window. Pure C, no ESP-IDF.
 *
 * The band has no RTC and no network: until the phone sends `set_time`, time() counts from
 * 1970 plus the uptime, so "the hour of day" is the hour since boot. The window used to be
 * evaluated anyway, which made every boot start inside the night for its first seven hours.
 * Compiled and run on the host (tests/firmware_host/host_glue_night_window.c).
 */
#ifndef SYNAPSE_NIGHT_WINDOW_H
#define SYNAPSE_NIGHT_WINDOW_H

#include <stdint.h>

/* 2020-01-01T00:00:00Z: the same minimum as the control point set_time
 * (config/hardware.yaml ble.consumer_v2.control_point.set_time_min_epoch_ms; a test compares them).
 * Not included from the generated header: this component does not depend on `ble`. */
#define SYNAPSE_CLOCK_MIN_EPOCH_S 1577836800LL

/** 1 when @p epoch_s is a real date: after the contract's minimum for set_time (2020-01-01). */
static inline int synapse_clock_is_set(int64_t epoch_s)
{
    return epoch_s >= SYNAPSE_CLOCK_MIN_EPOCH_S;
}

/**
 * 1 when @p epoch_s falls inside [start_hour, end_hour) in UTC; the window may wrap midnight
 * (23..7). 0 whenever the clock has not been set.
 * ponytail: UTC, not the wearer's local time (the band does not know the zone); send the
 * offset with set_time if the window must follow the local night.
 */
static inline int synapse_night_window_active(int64_t epoch_s, int start_hour, int end_hour)
{
    if (!synapse_clock_is_set(epoch_s)) {
        return 0;
    }
    const int hour = (int)((epoch_s / 3600) % 24);
    if (start_hour > end_hour) {
        return hour >= start_hour || hour < end_hour;
    }
    return hour >= start_hour && hour < end_hour;
}

#endif /* SYNAPSE_NIGHT_WINDOW_H */
