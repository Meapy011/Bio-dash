# A second person's strap shows up
- Fixed: a person's Heart worker could hang forever in its first search when two workers asked Bluetooth to scan at the same moment, so their page never listed any strap. Scans now give up after 10 s and retry; a worker that keeps hanging is started again
- Workers start 1.5 s apart, and the server restarts any Heart, Brain or Air worker that hasn't finished a search for 90 s
- Radio sharing: a connected device keeps the radio it's on, and a strap still searching gets a radio no other strap uses
- Settings → Bluetooth radios warns when two straps share a radio; a connected device's list says it's from before connecting
- Also keeps the Forecast-button fix from the previous patch
