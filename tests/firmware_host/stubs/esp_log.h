/* Host stub for esp_log.h: silences the ESP_LOGx macros. */
#ifndef STUB_ESP_LOG_H
#define STUB_ESP_LOG_H
#define ESP_LOGE(tag, fmt, ...) ((void)(tag))
#define ESP_LOGW(tag, fmt, ...) ((void)(tag))
#define ESP_LOGI(tag, fmt, ...) ((void)(tag))
#define ESP_LOGD(tag, fmt, ...) ((void)(tag))
#endif
