# Every dashboard in the V6 design, and the forecast models included
- Every dashboard in the V6 design: Heart, Oxygen, Air, Water, Omni, Analysis and Brain pages with the redesign's colours, page heads, cards, buttons and chart colours, in every theme
- The sidebar lists the dashboards by those names (Heart, Air, Oxygen, Water, Analysis, Brain, Omni)
- The forecast models are included (polar_dashboard/forecast/models: 49 + 69 MB), so a new release folder doesn't need them copied in
- PyTorch is still set up separately: on the Orin, tools/orin_setup.sh (reuses the system's Jetson PyTorch)
