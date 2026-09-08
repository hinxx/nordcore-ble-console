idf_component_register(
    SRCS "main.c" "ble_gatt.c"
    INCLUDE_DIRS "."
    REQUIRES driver esp_timer bt nvs_flash
)
