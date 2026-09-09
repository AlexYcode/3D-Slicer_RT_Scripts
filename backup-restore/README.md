# RTPlan Backup / Restore v5 for 3D Slicer 5.12.3 + SlicerRT 1.0.0

Designed for the Technologies in Radiotherapy project workflow using SlicerRT
and pyRadPlan.

IMPORTANT
---------
This script is intended for Slicer 5.12.3 with the SlicerRT revision used in this project (f57ccf3-era API). It might be backward-compatible with other SlicerRT versions, but that is not guaranteed. Use on your own risk.
It must be executed inside Slicer's Python Interactor, not in normal system Python.

**Under in the python script *Configuration* replace**

```python
DEFAULT_PARENT_DIR = (
    r"C:\your-project-directory\Backups"
)
```


**with your preferred backup location.**



Normal execution opens an interactive menu.  To load functions without opening
that menu, set RTPLAN_V5_NO_MENU=True before exec(...).

v5 changes compared with v4
---------------------------
* PLAN-NAME AGNOSTIC: if plan_name=None, backup uses the RT plan currently
  selected in External Beam Planning (for example RTPlan_Proton_3beam).
* Backup folder contains the plan name:
      <parent>/YYYYMMDD/<PlanName>_YYYYMMDD_HHMMSS
* Explicit plan_name still works and temporarily selects that plan in EBP.
* Restore picker accepts plan-specific v5 folder names and old RTPlan_* folders.
* Direct RTPlan API is used for multiple target IDs, BODY, Rx, ion flag,
  inverse flag, dose engine, optimizer, isocenter, and dose grid.
* Objectives are saved/restored through the current SlicerRT
  qMRMLObjectivesTableWidget layout (#, C, Objective, Segments, OP, p,
  Parameters), avoiding hard-coded widget-column assumptions where possible.
* Read-only validate_backup() checks a backup without changing the current scene.
* Optional optimizer-UI settings (e.g. IPOPT controls) are captured/restored.
* Incomplete backups remain suffixed _INCOMPLETE and are not offered normally.
* Backward-compatible loader for v4, v3, and legacy Plan_summary.json backups.


---

## How to use

It names the folder after the plan, for example:

```text
Backups\
└── 20260908\
    └── RTPlan_Proton_3beam_20260908_193000\
```

### 1. Normal use — interactive menu

Put the file in your `Helpfullscripts` folder and run:

**Please replace `C:\your-project-directory\` by the path to your `Helpfullscripts` folder (use CTRL+H)**

```python
exec(open(
    r"C:\your-project-directory\Helpfullscripts\RTPlan_Backup_Restore_v5_Slicer_5_12_3.py"
).read())
```

This opens a menu with options to back up the currently selected plan, back up a named plan, restore, validate a backup, or print the active-plan summary.

For your current proton plan, simply choose:

**Backup currently selected RT plan**

It should begin with something similar to:

```text
Using currently selected External Beam Planning RT plan: RTPlan_Proton_3beam

==============================================================================
RTPLAN BACKUP v5
==============================================================================
Plan: RTPlan_Proton_3beam
Ion plan: True
```

### 2. Load the script without opening the menu

Useful when you want to call its functions directly:

```python
RTPLAN_V5_NO_MENU = True

exec(open(
    r"C:\your-project-directory\Helpfullscripts\RTPlan_Backup_Restore_v5_Slicer_5_12_3.py"
).read())
```

Then you can use any function below.

### 3. Backup current selected plan — easiest

This asks you where to save it:

```python
backup_current_plan()
```

No plan name is needed.

If `RTPlan_Proton_3beam` is selected in External Beam Planning, that is what gets backed up.

### 4. Backup current plan without directory dialog

For your project directory:

```python
backup_current_plan(
    parent_dir=r"C:\your-project-directory\Backups"
)
```


It automatically creates today's directory:

```text
...\Backups\20260908\
```

and then, for example:

```text
RTPlan_Proton_3beam_20260908_193000
```

### 5. Explicitly select a particular plan

You can still override automatic selection:

```python
backup_current_plan(
    parent_dir=r"C:\your-project-directory\Backups",
    plan_name="RTPlan_Proton_3beam"
)
```

Or:

```python
backup_current_plan(
    parent_dir=r"C:\your-project-directory\Backups",
    plan_name="RTPlan"
)
```

### 6. All backup options

The full call is:

```python
backup_current_plan(
    parent_dir=r"C:\...\Backups",
    plan_name=None,
    save_dij=True,
    save_dose_volumes=True,
    use_daily_subfolder=True,
    dose_scope="all",
    dose_numpy_fallback=False
)
```

The important options are:

| Option                | Default | Meaning                                          |
| --------------------- | ------- | ------------------------------------------------ |
| `plan_name`           | `None`  | Use currently selected EBP plan                  |
| `save_dij`            | `True`  | Save each beam's dose-influence matrix           |
| `save_dose_volumes`   | `True`  | Attempt to save dose volumes                     |
| `use_daily_subfolder` | `True`  | Create `YYYYMMDD` folder                         |
| `dose_scope`          | `"all"` | Save all dose-like volumes in scene              |
| `dose_numpy_fallback` | `False` | If NRRD fails, optionally save voxel data as NPZ |

For now I recommend leaving `dose_scope="all"`. It is more conservative and avoids accidentally omitting a total dose whose name doesn't match the plan name.

I also left:

```python
dose_numpy_fallback=False
```

as the default because those per-beam dose arrays can become **very large**. If Slicer again refuses to save the individual proton beam dose NRRDs and we specifically want those archived, we can run:

```python
backup_current_plan(
    parent_dir=r"C:\your-project-directory\Backups",
    dose_numpy_fallback=True
)
```

That gives the script a second way to store a dose volume when the normal NRRD writer fails.

### 7. Check which plan would be backed up

Before making a backup, you can run:

```python
print_active_plan_summary()
```

For your present plan I expect roughly:

```text
ACTIVE RT PLAN
Plan: RTPlan_Proton_3beam
Ion plan: True
Inverse plan: True
...
Beams:
  Proton_1: gantry=0...
  Proton_2: gantry=50...
  Proton_3: gantry=310...
```

This is a useful safety check whenever you have both photon and proton plans open.

### 8. Validate a backup without restoring it

This remains completely read-only with respect to the Slicer scene:

```python
validate_backup(
    r"C:\your-project-directory\Backups\20260908\RTPlan_Proton_3beam_20260908_193000"
)
```

It checks the JSON, reference CT, active segmentation, DIJ files, dose files recorded in the JSON, beam/objective counts, and the completion marker.

It should finish with:

```text
RESULT: PASS
No nodes were loaded, removed, or modified by this validation.
```

### 9. Restore an exact backup

```python
restore_backup(
    r"C:\...\Backups\20260908\RTPlan_Proton_3beam_20260908_193000"
)
```

The restore defaults are deliberately conservative:

```python
restore_backup(
    backup_dir=r"C:\...\backup-folder",
    clear_scene=False,
    restore_dij=True,
    restore_dose_volumes=True,
    restore_all_segmentations=True,
    plan_name_override=None
)
```

In particular:

```python
clear_scene=False
```

means the script **does not delete your existing scene**.

If a plan with the same name already exists, v5 generates something such as:

```text
RTPlan_Proton_3beam_RESTORED_1
```

instead of overwriting your current plan.

### 10. Choose a restore from your whole Backups directory

```python
restore_from_parent(
    parent_dir=r"C:\your-project-directory\Backups"
)
```

You can also get these examples directly inside Slicer after loading the script:

```python
help_v5()
```
