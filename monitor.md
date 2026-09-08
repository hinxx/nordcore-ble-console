# idf monitor

--- based on miniterm from pySerial
--- 
--- Ctrl+]   Exit program
--- Ctrl+T   Menu escape key, followed by:
--- Menu keys:
---    Ctrl+T         Send the menu character itself to remote
---    Ctrl+]         Send the exit character itself to remote
---    Ctrl+R         Reset target board via RTS line
---    Ctrl+F         Build & flash project (fast reflash, ESP-IDF 6.1+)
---    Ctrl+A (or A)  Build & flash app only
---    Ctrl+E (or E)  Build & full flash project
---    Ctrl+Y         Toggle output display
---    Ctrl+L         Toggle saving output into file
---    Ctrl+I (or I)  Toggle printing timestamps
---    Ctrl+P         Reset target into bootloader via the DTR/RTS lines
---    Ctrl+X (or X)  Exit program
...--- esp_idf_monitor (1.10.0) - ESP-IDF Monitor tool

