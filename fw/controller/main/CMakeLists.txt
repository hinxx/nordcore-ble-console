idf_component_register(
    SRCS "main.c" "uart_tx.c" "uart_rx.c" "ble_gatt.c"
    INCLUDE_DIRS "."
    REQUIRES driver esp_timer bt nvs_flash
)
