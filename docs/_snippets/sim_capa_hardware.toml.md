```toml title="configs/hardware/sim_capa.toml (excerpt)"
name = "sim_capa"

# --- One [[devices]] entry per physical instrument or sim equivalent ---

[[devices]]
name = "heater"
adapter = "capa.devices.sim.watlow_sim"
# `params` is adapter-specific. Real Watlow: serial port, EZ-Zone address.
# Sim Watlow: declarative signals that ramp/step over the run.
[devices.params.signals."process_value/1"]
kind = "ramp"
start = 30.0
end = 600.0
duration_s = 0.2

[[devices]]
name = "purge_mfc"
adapter = "capa.devices.sim.alicat_sim"
[devices.params.signals.Mass_Flow]
kind = "constant"
value = 100.0

[[devices]]
name = "balance"
adapter = "capa.devices.sim.sartorius_sim"
[devices.params.mass_signal]
kind = "ramp"
start = 5.0
end = 4.0
duration_s = 0.2

[[devices]]
name = "cdaq1"
adapter = "capa.devices.sim.nidaq_polled_sim"
[devices.params.signals.TC_top_1]
kind = "ramp"
start = 303.0
end = 873.0
duration_s = 0.2

# --- One [[channels]] entry per named scientific signal ---

[[channels]]
name = "heater.pv"
kind = "process_var"
unit = "degC"
plot_group = "temperatures"
[channels.metadata]
capa_group = "heater_pv"  # CAPA-profile role
[channels.source]
source = "watlow_parameter"
device = "heater"
parameter = "process_value"
instance = 1
[channels.calibration]
kind = "identity"
input_unit = "degC"
output_unit = "degC"

[[channels]]
name = "purge.flow"
kind = "mfc_flow"
unit = "slpm"
plot_group = "flows"
[channels.metadata]
capa_group = "purge_gas_flow"
[channels.source]
source = "alicat_frame_field"
device = "purge_mfc"
field = "Mass_Flow"
[channels.calibration]
kind = "identity"
input_unit = "slpm"
output_unit = "slpm"

[[channels]]
name = "TC_sample_top"
kind = "tc"
unit = "K"
plot_group = "temperatures"
[channels.metadata]
capa_group = "sample_temperature"
[channels.source]
source = "nidaq_reading_field"
device = "cdaq1"
task = "default_task"
field = "TC_top_1"
[channels.calibration]
kind = "identity"
input_unit = "K"
output_unit = "K"

[[channels]]
name = "balance.mass"
kind = "mass"
unit = "g"
plot_group = "mass"
[channels.metadata]
capa_group = "mass"
[channels.source]
source = "sartorius_reading"
device = "balance"
field = "value"
[channels.calibration]
kind = "identity"
input_unit = "g"
output_unit = "g"
```
