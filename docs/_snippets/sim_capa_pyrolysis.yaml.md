```yaml title="configs/experiments/sim_capa_pyrolysis.yaml"
# References to external files resolve relative to this YAML's directory.
hardware: ../hardware/sim_capa.toml
method:   ../methods/sim_capa_pyrolysis.method.toml

procedure:
  id: capa.builtin.recipe_runner   # plugin id; matched against plugins.lock
  version: "0.1"
  config:
    auto_acknowledge_prompts: true
    notes: "CAPA sim smoke: ramp under N2, soak, cool down."

domain_profile:
  id: capa.profiles.capa_pyrolysis  # CAPA scientific layer
  metadata:
    specimen:
      id: SIM-PMMA-001
      material: PMMA
      initial_mass_g: 5.0
      form: disk
      specimen_holder: "stainless steel cup"
      conditioning: "23C / 50% RH for 48h"
    program:
      target_heat_flux_kw_m2: 50.0
      heater_setpoint_c: 600.0
    atmosphere:
      mode: inert
      purge_duration_s: 30.0
      purge:
        species: N2
        purity: "UHP 5.0"
        target_flow_sccm: 100.0

calibration_set:
  name: default

operator:
  id: abr
  display_name: A. Researcher

sample:
  id: SIM-CAPA-001
  material: PMMA
  notes: "CAPA sim smoke run"

tags: [sim, capa, pyrolysis]
```
