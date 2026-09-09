# -*- coding: utf-8 -*-
"""
RTPlan Backup / Restore v5 for 3D Slicer 5.12.3 + SlicerRT
=============================================================

Designed for the Technologies in Radiotherapy project workflow using SlicerRT
and pyRadPlan.

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

IMPORTANT
---------
This script is intended for Slicer 5.12.3 with the SlicerRT revision used in
this project (f57ccf3-era API). It might be backward-compatible with other SlicerRT versions, but that is not guaranteed. Use on your own risk.  Always verify the restored plan and dose distributions before clinical use.
It must be executed inside Slicer's Python
Interactor, not in normal system Python.

Under *Configuration* replace 'C:\your\project-directory\Backups' with your preferred backup location. 
The script will create a subfolder for each backup with the current date and time. 

Normal execution opens an interactive menu.  To load functions without opening
that menu, set RTPLAN_V5_NO_MENU=True before exec(...).
"""

import os
import re
import json
import zipfile
import traceback
from datetime import datetime

import numpy as np
import vtk
import qt
import slicer

try:
    from scipy.sparse import csc_matrix, save_npz, load_npz
    SCIPY_SPARSE_AVAILABLE = True
except Exception:
    csc_matrix = save_npz = load_npz = None
    SCIPY_SPARSE_AVAILABLE = False


# ==============================================================================
# Configuration
# ==============================================================================

SCRIPT_VERSION = 5
BACKUP_SCHEMA_VERSION = 5
BACKUP_FILE = "RTPlan_backup_v5.json"
PREVIOUS_BACKUP_FILES = ["RTPlan_backup_v4.json", "RTPlan_backup_v3.json"]
COMPATIBILITY_FILE = "Plan_summary.json"
BACKUP_COMPLETE_MARKER = "BACKUP_COMPLETE.txt"
BACKUP_STATUS_FILE = "Backup_status.json"

DEFAULT_PARENT_DIR = (
    r"C:\your-project-directory\Backups"
)

# Matches both v5 plan-specific folders and legacy RTPlan_YYYYMMDD_HHMMSS.
BACKUP_FOLDER_REGEX = re.compile(r"^.+_\d{8}_\d{6}(?:_\d{2})?$")


# ==============================================================================
# Small helpers
# ==============================================================================

def _value_or_call(value):
    try:
        return value() if callable(value) else value
    except Exception:
        return value


def qtext(obj):
    if obj is None:
        return ""
    value = getattr(obj, "text", "")
    try:
        return str(_value_or_call(value))
    except Exception:
        return ""


def qobject_name(obj):
    if obj is None:
        return ""
    value = getattr(obj, "objectName", "")
    try:
        return str(_value_or_call(value))
    except Exception:
        return ""


def qrow_count(table):
    return int(_value_or_call(getattr(table, "rowCount", 0)))


def qcol_count(table):
    return int(_value_or_call(getattr(table, "columnCount", 0)))


def qcombo_count(combo):
    return int(_value_or_call(getattr(combo, "count", 0)))


def qcombo_current_text(combo):
    return str(_value_or_call(getattr(combo, "currentText", "")))


def qspin_value(widget):
    return _value_or_call(getattr(widget, "value", 0))


def process_events(iterations=3):
    for _ in range(max(1, int(iterations))):
        try:
            slicer.app.processEvents()
        except Exception:
            try:
                qt.QApplication.processEvents()
            except Exception:
                pass


def normalize_path(path):
    if path is None:
        return None
    return os.path.normpath(os.path.expanduser(str(path).strip()))


def safe_filename(name, fallback="RTPlan"):
    name = str(name or fallback).strip()
    name = re.sub(r"[<>:\"/\\|?*]+", "_", name)
    name = re.sub(r"\s+", "_", name)
    name = re.sub(r"_+", "_", name).strip("._ ")
    return name or fallback


def node_attributes(node):
    result = {}
    if node is None:
        return result
    try:
        names = vtk.vtkStringArray()
        node.GetAttributeNames(names)
        for i in range(names.GetNumberOfValues()):
            key = names.GetValue(i)
            result[key] = node.GetAttribute(key)
    except Exception:
        try:
            for key in node.GetAttributeNames():
                result[str(key)] = node.GetAttribute(str(key))
        except Exception:
            pass
    return result


def restore_node_attributes(node, attributes):
    if node is None:
        return
    for key, value in (attributes or {}).items():
        try:
            node.SetAttribute(str(key), None if value is None else str(value))
        except Exception:
            pass


def choose_directory(title, initial_dir=None):
    initial_dir = normalize_path(initial_dir or DEFAULT_PARENT_DIR)
    result = qt.QFileDialog.getExistingDirectory(
        slicer.util.mainWindow(), title, initial_dir
    )
    result = str(result) if result else ""
    return normalize_path(result) if result else None


def qinput_get_item(title, label, items, current=0, editable=False):
    """Compatibility wrapper for Slicer/PythonQt QInputDialog.getItem."""
    result = qt.QInputDialog.getItem(
        slicer.util.mainWindow(), title, label, list(items), current, editable
    )
    if isinstance(result, (tuple, list)):
        if len(result) >= 2:
            return str(result[0]), bool(result[1])
        if len(result) == 1:
            text = str(result[0])
            return text, bool(text)
        return "", False
    text = str(result) if result is not None else ""
    return text, bool(text)


def ask_yes_no(title, text, default_yes=True):
    default_button = qt.QMessageBox.Yes if default_yes else qt.QMessageBox.No
    answer = qt.QMessageBox.question(
        slicer.util.mainWindow(),
        title,
        text,
        qt.QMessageBox.Yes | qt.QMessageBox.No,
        default_button,
    )
    return answer == qt.QMessageBox.Yes


def _valid_grid(values):
    try:
        vals = [float(v) for v in values]
    except Exception:
        return False
    return len(vals) == 3 and all(np.isfinite(v) and v > 0.0 for v in vals)


def _grid_list(values):
    return [float(v) for v in values] if _valid_grid(values) else None


def _grid_close(a, b, atol=1e-4):
    if not (_valid_grid(a) and _valid_grid(b)):
        return False
    return bool(np.allclose(np.asarray(a), np.asarray(b), rtol=0.0, atol=atol))


def resolve_file(backup_dir, path_value):
    if not path_value:
        return None
    path_value = str(path_value)
    if os.path.isabs(path_value):
        return normalize_path(path_value)
    return normalize_path(os.path.join(backup_dir, path_value))


def resolve_backup_parent(parent_dir, use_daily_subfolder=True):
    parent_dir = normalize_path(parent_dir)
    if not use_daily_subfolder:
        return parent_dir
    if re.fullmatch(r"\d{8}", os.path.basename(parent_dir)):
        return parent_dir
    return os.path.join(parent_dir, datetime.now().strftime("%Y%m%d"))


# ==============================================================================
# External Beam Planning / RT plan selection
# ==============================================================================

def get_external_beam_planning_widget(select_module=False):
    if select_module:
        try:
            slicer.util.selectModule("ExternalBeamPlanning")
            process_events(4)
        except Exception:
            pass
    module = getattr(slicer.modules, "externalbeamplanning", None)
    if module is None:
        raise RuntimeError("External Beam Planning module is not available.")
    widget = module.widgetRepresentation()
    if widget is None:
        raise RuntimeError("External Beam Planning widget is not available.")
    return widget


def get_active_rt_plan_from_ebp():
    """Return the RT plan currently selected in External Beam Planning."""
    try:
        ebp = get_external_beam_planning_widget(select_module=False)
    except Exception:
        return None

    # Preferred exact selector from current SlicerRT UI.
    try:
        combo = ebp.findChild(slicer.qMRMLNodeComboBox, "MRMLNodeComboBox_RtPlan")
        if combo is not None:
            node = combo.currentNode()
            if node is not None and node.IsA("vtkMRMLRTPlanNode"):
                return node
    except Exception:
        pass

    # Fallback scan.
    candidates = {}
    try:
        for combo in ebp.findChildren(slicer.qMRMLNodeComboBox):
            try:
                node = combo.currentNode()
                if node is not None and node.IsA("vtkMRMLRTPlanNode"):
                    candidates[node.GetID()] = node
            except Exception:
                pass
    except Exception:
        pass

    if len(candidates) == 1:
        return next(iter(candidates.values()))
    return None


def list_rt_plans():
    return list(slicer.util.getNodesByClass("vtkMRMLRTPlanNode"))


def find_plan(plan_name=None):
    """
    Select an RT plan using this priority:
      1) explicit plan_name if supplied;
      2) current External Beam Planning selection;
      3) the only RT plan in the scene.
    Never silently guesses among multiple plans.
    """
    if plan_name:
        matches = [p for p in list_rt_plans() if p.GetName() == str(plan_name)]
        if len(matches) == 1:
            print("Using explicitly requested RT plan:", matches[0].GetName())
            return matches[0]
        if len(matches) > 1:
            raise RuntimeError(
                f"More than one RT plan is named '{plan_name}'. Rename plans so names are unique."
            )
        raise RuntimeError(f"Requested RT plan '{plan_name}' was not found.")

    active = get_active_rt_plan_from_ebp()
    if active is not None:
        print("Using currently selected External Beam Planning RT plan:", active.GetName())
        return active

    plans = list_rt_plans()
    if len(plans) == 1:
        print("Only one RT plan exists; using:", plans[0].GetName())
        return plans[0]
    if not plans:
        raise RuntimeError("No vtkMRMLRTPlanNode exists in the current scene.")

    raise RuntimeError(
        "More than one RTPlan exists and the active plan could not be determined.\n"
        "Available plans:\n  - " + "\n  - ".join(p.GetName() for p in plans) +
        "\nSelect the desired plan in External Beam Planning or pass plan_name='...'."
    )


def set_active_plan_in_ebp(plan, ebp=None):
    ebp = ebp or get_external_beam_planning_widget(select_module=True)
    combo = ebp.findChild(slicer.qMRMLNodeComboBox, "MRMLNodeComboBox_RtPlan")
    if combo is None:
        raise RuntimeError("Could not find External Beam Planning RT plan selector.")
    combo.setCurrentNode(plan)
    process_events(5)
    return combo


# ==============================================================================
# Beam / DIJ helpers
# ==============================================================================

def get_plan_beams(plan):
    collection = vtk.vtkCollection()
    try:
        plan.GetBeams(collection)
        return [collection.GetItemAsObject(i) for i in range(collection.GetNumberOfItems())]
    except Exception:
        # Fallback by beam number scan.
        beams = []
        wanted = int(plan.GetNumberOfBeams())
        number = 0
        while len(beams) < wanted and number < 10000:
            beam = plan.GetBeamByNumber(number)
            if beam is not None:
                beams.append(beam)
            number += 1
        return beams


def beam_dij_to_sparse(beam):
    if not SCIPY_SPARSE_AVAILABLE:
        raise RuntimeError("scipy.sparse is unavailable in this Slicer Python environment.")
    nnz = int(beam.GetDoseInfluenceMatrixNumberOfNonZeroElements())
    if nnz <= 0:
        return None
    field_data = beam.GetDoseInfluenceMatrixFieldData()
    if field_data is None:
        return None
    data_array = field_data.GetArray("Data")
    indices_array = field_data.GetArray("Indices")
    indptr_array = field_data.GetArray("Indptr")
    if not data_array or not indices_array or not indptr_array:
        return None
    data = np.asarray(data_array, dtype=np.float64)
    indices = np.asarray(indices_array, dtype=np.int32)
    indptr = np.asarray(indptr_array, dtype=np.int32)
    rows = int(beam.GetDoseInfluenceMatrixRowCount())
    cols = int(beam.GetDoseInfluenceMatrixColumnCount())
    if rows <= 0 or cols <= 0:
        return None
    return csc_matrix((data, indices, indptr), shape=(rows, cols))


def save_beam_dij(beam, backup_dir):
    matrix = beam_dij_to_sparse(beam)
    if matrix is None:
        return None
    filename = f"dij_{safe_filename(beam.GetName(), 'Beam')}.npz"
    path = os.path.join(backup_dir, filename)
    save_npz(path, matrix)
    return filename


def load_beam_dij(beam, backup_dir, record):
    rel = record.get("dij_file")
    if not rel:
        return False
    path = resolve_file(backup_dir, rel)
    if not path or not os.path.isfile(path):
        print("WARNING: DIJ file missing for", beam.GetName(), ":", path)
        return False
    if not SCIPY_SPARSE_AVAILABLE:
        print("WARNING: scipy.sparse unavailable; DIJ restore skipped for", beam.GetName())
        return False
    dij = load_npz(path).tocoo()
    dim = record.get("dose_grid_dim")
    spacing = record.get("dose_grid_spacing_mm")
    if not (dim and len(dim) == 3 and _valid_grid(spacing)):
        print("WARNING: missing dose-grid metadata; DIJ restore skipped for", beam.GetName())
        return False
    rows = np.asarray(dij.row, dtype=np.int32)
    cols = np.asarray(dij.col, dtype=np.int32)
    values = np.asarray(dij.data, dtype=np.float64)
    beam.SetDoseInfluenceMatrixFromTriplets(
        int(dij.shape[0]), int(dij.shape[1]), rows, cols, values,
        [int(v) for v in dim], [float(v) for v in spacing]
    )
    return True


# ==============================================================================
# Dose-grid helpers
# ==============================================================================

def get_ui_dose_grid_spacing(ebp):
    """Best-effort read of the 3 visible dose-grid QDoubleSpinBoxes."""
    if ebp is None:
        return None, []
    candidates = []
    try:
        widgets = ebp.findChildren(qt.QDoubleSpinBox)
    except Exception:
        widgets = []
    for w in widgets:
        try:
            name = qobject_name(w)
            tooltip = str(_value_or_call(getattr(w, "toolTip", "")))
            status_tip = str(_value_or_call(getattr(w, "statusTip", "")))
            value = float(qspin_value(w))
        except Exception:
            continue
        text = " ".join([name, tooltip, status_tip]).lower()
        if "grid" in text or ("dose" in text and "spacing" in text):
            candidates.append({
                "name": name,
                "value": value,
                "tooltip": tooltip,
                "status_tip": status_tip,
            })

    axis_map = {}
    for item in candidates:
        n = item["name"].lower()
        for axis in ("x", "y", "z"):
            if (n.endswith(axis) or f"_{axis}" in n or f"{axis}spacing" in n
                    or f"spacing{axis}" in n or f"grid{axis}" in n or f"{axis}grid" in n):
                axis_map.setdefault(axis, item["value"])
    if all(a in axis_map for a in ("x", "y", "z")):
        grid = [axis_map["x"], axis_map["y"], axis_map["z"]]
        return (_grid_list(grid), candidates)
    if len(candidates) == 3:
        return (_grid_list([x["value"] for x in candidates]), candidates)
    return None, candidates


def set_ui_dose_grid_spacing(ebp, grid):
    if ebp is None or not _valid_grid(grid):
        return False
    try:
        widgets = ebp.findChildren(qt.QDoubleSpinBox)
    except Exception:
        return False
    candidates = []
    for w in widgets:
        try:
            name = qobject_name(w)
            tooltip = str(_value_or_call(getattr(w, "toolTip", "")))
            status_tip = str(_value_or_call(getattr(w, "statusTip", "")))
        except Exception:
            continue
        text = " ".join([name, tooltip, status_tip]).lower()
        if "grid" in text or ("dose" in text and "spacing" in text):
            candidates.append(w)
    axis_widgets = {}
    for w in candidates:
        n = qobject_name(w).lower()
        for axis in ("x", "y", "z"):
            if (n.endswith(axis) or f"_{axis}" in n or f"{axis}spacing" in n
                    or f"spacing{axis}" in n or f"grid{axis}" in n or f"{axis}grid" in n):
                axis_widgets.setdefault(axis, w)
    if all(a in axis_widgets for a in ("x", "y", "z")):
        for axis, val in zip(("x", "y", "z"), grid):
            axis_widgets[axis].setValue(float(val))
        process_events(3)
        return True
    if len(candidates) == 3:
        for w, val in zip(candidates, grid):
            w.setValue(float(val))
        process_events(3)
        return True
    return False


def collect_dose_grid_state(plan, ebp=None):
    plan_grid = None
    try:
        plan_grid = _grid_list(plan.GetDoseGridSpacing())
    except Exception:
        pass
    ui_grid, ui_candidates = get_ui_dose_grid_spacing(ebp)
    beam_records = []
    dij_grids = []
    all_beam_grids = []
    for beam in get_plan_beams(plan):
        grid = None
        dim = None
        nnz = 0
        try:
            grid = _grid_list(beam.GetDoseGridSpacing())
        except Exception:
            pass
        try:
            dim = [int(v) for v in beam.GetDoseGridDim()]
        except Exception:
            pass
        try:
            nnz = int(beam.GetDoseInfluenceMatrixNumberOfNonZeroElements())
        except Exception:
            pass
        beam_records.append({
            "name": beam.GetName(), "spacing_mm": grid, "dim": dim, "dij_nnz": nnz
        })
        if grid:
            all_beam_grids.append(grid)
            if nnz > 0:
                dij_grids.append(grid)

    def consistent(grids):
        if not grids:
            return None
        return list(grids[0]) if all(_grid_close(grids[0], x) for x in grids[1:]) else None

    dij_grid = consistent(dij_grids)
    beam_grid = consistent(all_beam_grids)
    if dij_grid:
        effective, source = dij_grid, "beam_DIJ"
    elif ui_grid:
        effective, source = ui_grid, "ExternalBeamPlanning_UI"
    elif plan_grid:
        effective, source = plan_grid, "RTPlan_node"
    elif beam_grid:
        effective, source = beam_grid, "beam_node"
    else:
        effective, source = None, "unavailable"

    warnings = []
    known = [("beam_DIJ", dij_grid), ("UI", ui_grid), ("plan", plan_grid), ("beam", beam_grid)]
    known = [(n, g) for n, g in known if g]
    for i in range(len(known)):
        for j in range(i + 1, len(known)):
            if not _grid_close(known[i][1], known[j][1], atol=1e-3):
                warnings.append(
                    f"Dose-grid mismatch: {known[i][0]}={known[i][1]} vs {known[j][0]}={known[j][1]}"
                )
    return {
        "effective_spacing_mm": effective,
        "effective_source": source,
        "plan_spacing_mm": plan_grid,
        "ui_spacing_mm": ui_grid,
        "ui_candidates": ui_candidates,
        "beam_grids": beam_records,
        "warnings": warnings,
    }


# ==============================================================================
# Objective table helpers (current SlicerRT layout)
# ==============================================================================

def get_objectives_widget(ebp):
    obj_widget = ebp.findChild(qt.QWidget, "ObjectivesTableWidget")
    if obj_widget is None:
        # Fallback scan by object name.
        for w in ebp.findChildren(qt.QWidget):
            if qobject_name(w) == "ObjectivesTableWidget":
                obj_widget = w
                break
    return obj_widget


def get_objectives_table(ebp):
    obj_widget = get_objectives_widget(ebp)
    if obj_widget is None:
        return None, None
    table = obj_widget.findChild(qt.QTableWidget, "ObjectivesTable")
    return obj_widget, table


def objective_column_map(table):
    """Map semantic roles to table indices from visible header text."""
    role = {}
    for col in range(qcol_count(table)):
        item = table.horizontalHeaderItem(col)
        text = qtext(item).strip().lower()
        if text == "#":
            role["number"] = col
        elif text == "c":
            role["constraint"] = col
        elif text == "objective":
            role["objective"] = col
        elif text.startswith("segment"):
            role["segment"] = col
        elif text == "op":
            role["op"] = col
        elif text == "p":
            role["penalty"] = col
        elif text.startswith("parameter"):
            role["parameters"] = col
    # Current f57ccf3 fallback order.
    defaults = {
        "number": 0, "constraint": 1, "objective": 2, "segment": 3,
        "op": 4, "penalty": 5, "parameters": 6,
    }
    for k, v in defaults.items():
        role.setdefault(k, v if v < qcol_count(table) else None)
    return role


def parameter_widget_values(widget):
    result = {}
    if widget is None:
        return result
    try:
        labels = list(widget.findChildren(qt.QLabel))
        edits = list(widget.findChildren(qt.QLineEdit))
    except Exception:
        return result
    for label, edit in zip(labels, edits):
        name = qtext(label).strip()
        if name:
            result[name] = qtext(edit)
    return result


def set_parameter_widget_values(widget, values):
    if widget is None:
        return
    try:
        labels = list(widget.findChildren(qt.QLabel))
        edits = list(widget.findChildren(qt.QLineEdit))
    except Exception:
        return
    by_name = {qtext(label).strip(): edit for label, edit in zip(labels, edits)}
    for key, value in (values or {}).items():
        edit = by_name.get(str(key))
        if edit is None:
            print(f"WARNING: objective parameter '{key}' not present in current UI.")
            continue
        edit.setText(str(value))
        process_events(1)


def save_objective_table(plan, ebp):
    # Ensure the requested plan is what the UI/table is using.
    set_active_plan_in_ebp(plan, ebp)
    obj_widget, table = get_objectives_table(ebp)
    if obj_widget is None or table is None:
        raise RuntimeError("Current SlicerRT ObjectivesTableWidget/ObjectivesTable was not found.")
    try:
        obj_widget.setPlanNode(plan)
        obj_widget.setSegmentationNode(plan.GetSegmentationNode())
        process_events(3)
    except Exception:
        pass

    cols = objective_column_map(table)
    rows = []
    for r in range(qrow_count(table)):
        objective_combo = table.cellWidget(r, cols["objective"])
        segment_combo = table.cellWidget(r, cols["segment"])
        op_widget = table.cellWidget(r, cols["op"])
        penalty_widget = table.cellWidget(r, cols["penalty"])
        param_widget = table.cellWidget(r, cols["parameters"])
        constraint_widget = table.cellWidget(r, cols["constraint"]) if cols.get("constraint") is not None else None

        is_constraint = False
        if constraint_widget is not None:
            try:
                is_constraint = bool(constraint_widget.isChecked())
            except Exception:
                pass

        segment_id = ""
        try:
            data = segment_combo.currentData()
            segment_id = str(data.toString()) if hasattr(data, "toString") else str(data)
        except Exception:
            pass

        rows.append({
            "is_constraint": is_constraint,
            "name": qcombo_current_text(objective_combo),
            "segment_name": qcombo_current_text(segment_combo),
            "segment_id": segment_id,
            "overlap_priority": int(qspin_value(op_widget)),
            "penalty": int(qspin_value(penalty_widget)),
            "parameters": parameter_widget_values(param_widget),
        })
    return rows


def _combo_find_data_string(combo, target):
    target = str(target or "")
    if not target:
        return -1
    for i in range(qcombo_count(combo)):
        try:
            data = combo.itemData(i)
            text = str(data.toString()) if hasattr(data, "toString") else str(data)
            if text == target:
                return i
        except Exception:
            pass
    return -1


def refresh_optimizer_ui(ebp, optimizer_name):
    combo = ebp.findChild(qt.QComboBox, "comboBox_PlanOptimizer")
    if combo is None or not optimizer_name:
        return False
    idx = combo.findText(str(optimizer_name))
    if idx < 0:
        return False
    # Switch away and back if possible so the registry/objective list refreshes.
    current = int(_value_or_call(getattr(combo, "currentIndex", -1)))
    if current == idx and qcombo_count(combo) > 1:
        other = 0 if idx != 0 else 1
        combo.setCurrentIndex(other)
        process_events(2)
    combo.setCurrentIndex(idx)
    process_events(5)
    return True


def restore_objective_table(plan, ebp, rows):
    set_active_plan_in_ebp(plan, ebp)
    refresh_optimizer_ui(ebp, plan.GetPlanOptimizerName())
    obj_widget, table = get_objectives_table(ebp)
    if obj_widget is None or table is None:
        raise RuntimeError("Current SlicerRT ObjectivesTableWidget/ObjectivesTable was not found.")

    obj_widget.setPlanNode(plan)
    obj_widget.setSegmentationNode(plan.GetSegmentationNode())
    process_events(5)
    try:
        obj_widget.deleteObjectivesTable()
        process_events(3)
    except Exception:
        # Fallback row-by-row deletion via widget method if available.
        try:
            while qrow_count(table) > 0:
                obj_widget.removeRowFromRowIndex(qrow_count(table) - 1)
        except Exception:
            table.setRowCount(0)
    cols = objective_column_map(table)

    for index, saved in enumerate(rows or [], start=1):
        before = qrow_count(table)
        obj_widget.onObjectiveAdded()
        process_events(2)
        row = qrow_count(table) - 1
        if row < before:
            raise RuntimeError(f"Failed to add objective row {index}.")

        constraint_widget = table.cellWidget(row, cols["constraint"]) if cols.get("constraint") is not None else None
        if constraint_widget is not None:
            try:
                constraint_widget.setChecked(bool(saved.get("is_constraint", False)))
                process_events(2)
            except Exception:
                pass

        objective_combo = table.cellWidget(row, cols["objective"])
        desired_objective = str(saved.get("name", ""))
        obj_idx = objective_combo.findText(desired_objective)
        if obj_idx < 0:
            raise RuntimeError(
                f"Objective type '{desired_objective}' is not available in optimizer '{plan.GetPlanOptimizerName()}'."
            )
        objective_combo.setCurrentIndex(obj_idx)
        process_events(2)

        # Objective change reconstructs the row widgets, so fetch them again.
        segment_combo = table.cellWidget(row, cols["segment"])
        seg_idx = _combo_find_data_string(segment_combo, saved.get("segment_id"))
        if seg_idx < 0:
            seg_idx = segment_combo.findText(str(saved.get("segment_name", "")))
        if seg_idx < 0:
            raise RuntimeError(
                f"Segment '{saved.get('segment_name')}' not found while restoring objective {index}."
            )
        segment_combo.setCurrentIndex(seg_idx)
        process_events(2)

        op_widget = table.cellWidget(row, cols["op"])
        penalty_widget = table.cellWidget(row, cols["penalty"])
        op_widget.setValue(int(saved.get("overlap_priority", 0)))
        penalty_widget.setValue(int(saved.get("penalty", 10)))
        process_events(1)

        param_widget = table.cellWidget(row, cols["parameters"])
        set_parameter_widget_values(param_widget, saved.get("parameters", {}))
        process_events(2)

        print(
            f"  {index:2d}. {desired_objective} | {saved.get('segment_name')} | "
            f"OP={saved.get('overlap_priority')} | p={saved.get('penalty')} | "
            f"{saved.get('parameters', {})}"
        )

    try:
        obj_widget.setObjectivesInPlanOptimizer()
    except Exception:
        pass
    process_events(4)
    return qrow_count(table)


# ==============================================================================
# Optimizer UI settings
# ==============================================================================

def collect_optimizer_ui_settings(ebp):
    result = {}
    prefixes = ("spinBox_Ipopt_", "doubleSpinBox_Ipopt_", "lineEdit_Ipopt_")
    for cls in (qt.QSpinBox, qt.QDoubleSpinBox, qt.QLineEdit):
        try:
            widgets = ebp.findChildren(cls)
        except Exception:
            widgets = []
        for w in widgets:
            name = qobject_name(w)
            if not name.startswith(prefixes):
                continue
            if name.startswith("lineEdit_"):
                value = qtext(w)
            else:
                value = qspin_value(w)
            result[name] = value
    return result


def restore_optimizer_ui_settings(ebp, settings):
    for name, value in (settings or {}).items():
        w = ebp.findChild(qt.QWidget, str(name))
        if w is None:
            continue
        try:
            if hasattr(w, "setText") and str(name).startswith("lineEdit_"):
                w.setText(str(value))
            elif hasattr(w, "setValue"):
                w.setValue(value)
        except Exception:
            pass
    process_events(2)


# ==============================================================================
# Volume save helpers
# ==============================================================================

def is_valid_scalar_volume(node):
    if node is None:
        return False, None
    img = node.GetImageData()
    if img is None:
        return False, None
    dims = list(img.GetDimensions())
    comps = int(img.GetNumberOfScalarComponents())
    if len(dims) != 3 or any(int(v) <= 0 for v in dims) or comps <= 0:
        return False, None
    return True, {
        "dimensions": [int(v) for v in dims],
        "components": comps,
        "range": [float(v) for v in img.GetScalarRange()],
    }


def looks_like_dose_volume(node):
    name = (node.GetName() or "").lower()
    return (
        node.GetAttribute("DicomRtImport.DoseVolume") == "1"
        or "dose" in name
        or "photon" in name
        or "proton" in name
        or "pyradplanoptimized" in name
    )


def save_volume_numpy_fallback(node, path):
    arr = np.asarray(slicer.util.arrayFromVolume(node))
    matrix = vtk.vtkMatrix4x4()
    node.GetIJKToRASMatrix(matrix)
    mat = np.array([[matrix.GetElement(r, c) for c in range(4)] for r in range(4)], dtype=float)
    np.savez_compressed(path, array=arr, ijk_to_ras=mat)
    return True


def load_volume_numpy_fallback(path, name, attributes=None):
    data = np.load(path)
    arr = data["array"]
    mat = data["ijk_to_ras"]
    node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", str(name))
    slicer.util.updateVolumeFromArray(node, arr)
    matrix = vtk.vtkMatrix4x4()
    for r in range(4):
        for c in range(4):
            matrix.SetElement(r, c, float(mat[r, c]))
    node.SetIJKToRASMatrix(matrix)
    restore_node_attributes(node, attributes)
    return node


# ==============================================================================
# Backup
# ==============================================================================

def backup_current_plan(
    parent_dir=None,
    plan_name=None,
    save_dij=True,
    save_dose_volumes=True,
    use_daily_subfolder=True,
    dose_scope="all",
    dose_numpy_fallback=False,
):
    """
    Backup an RT plan.

    plan_name=None          -> use the plan currently selected in EBP.
    parent_dir=None         -> ask for the backup parent directory.
    save_dij=True           -> save beam dose-influence matrices.
    save_dose_volumes=True  -> save valid dose-like scalar volumes.
    use_daily_subfolder=True-> create/use YYYYMMDD below parent.
    dose_scope="all"        -> save all dose-like volumes; "related" restricts
                               to active-plan-looking names/output node.
    dose_numpy_fallback=False -> if NRRD export fails, optionally save raw voxel
                                 array + IJK-to-RAS as compressed NPZ.
    """
    if parent_dir is None:
        parent_dir = choose_directory("Choose parent directory for RTPlan backups", DEFAULT_PARENT_DIR)
        if not parent_dir:
            print("Backup cancelled.")
            return None
    parent_dir = resolve_backup_parent(parent_dir, use_daily_subfolder)
    os.makedirs(parent_dir, exist_ok=True)

    plan = find_plan(plan_name)
    segmentation_node = plan.GetSegmentationNode()
    reference_node = plan.GetReferenceVolumeNode()
    if segmentation_node is None:
        raise RuntimeError(f"Plan '{plan.GetName()}' has no segmentation node.")
    if reference_node is None:
        raise RuntimeError(f"Plan '{plan.GetName()}' has no reference volume node.")

    ebp = get_external_beam_planning_widget(select_module=True)
    active_before = get_active_rt_plan_from_ebp()
    set_active_plan_in_ebp(plan, ebp)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    plan_folder_stem = safe_filename(plan.GetName(), "RTPlan")
    final_backup_dir = os.path.join(parent_dir, f"{plan_folder_stem}_{timestamp}")
    incomplete_dir = final_backup_dir + "_INCOMPLETE"
    suffix = 1
    while os.path.exists(final_backup_dir) or os.path.exists(incomplete_dir):
        final_backup_dir = os.path.join(parent_dir, f"{plan_folder_stem}_{timestamp}_{suffix:02d}")
        incomplete_dir = final_backup_dir + "_INCOMPLETE"
        suffix += 1
    os.makedirs(incomplete_dir, exist_ok=False)
    backup_dir = incomplete_dir

    def write_status(state, message="", extra=None):
        payload = {
            "state": state,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "message": str(message),
        }
        if extra:
            payload.update(extra)
        try:
            with open(os.path.join(backup_dir, BACKUP_STATUS_FILE), "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
        except Exception:
            pass

    write_status("INCOMPLETE", "Backup started.")

    print("\n" + "=" * 78)
    print("RTPLAN BACKUP v5")
    print("=" * 78)
    print("Plan:", plan.GetName())
    print("Ion plan:", bool(plan.GetIonPlanFlag()))
    print("Writing temporary folder:", backup_dir)

    try:
        grid_state = collect_dose_grid_state(plan, ebp)
        print("\nDose grid:")
        print("  effective:", grid_state["effective_spacing_mm"])
        print("  source   :", grid_state["effective_source"])
        print("  RTPlan   :", grid_state["plan_spacing_mm"])
        print("  EBP UI   :", grid_state["ui_spacing_mm"])
        for warning in grid_state["warnings"]:
            print("  WARNING:", warning)

        # Reference CT
        reference_filename = f"ReferenceVolume_{safe_filename(reference_node.GetName(), 'CT')}.nrrd"
        reference_path = os.path.join(backup_dir, reference_filename)
        if not slicer.util.saveNode(reference_node, reference_path):
            raise RuntimeError("Failed to save reference CT.")

        # All segmentations
        seg_folder = os.path.join(backup_dir, "Segmentations")
        os.makedirs(seg_folder, exist_ok=True)
        segmentation_records = []
        for i, node in enumerate(slicer.util.getNodesByClass("vtkMRMLSegmentationNode"), start=1):
            filename = f"{i:02d}_{safe_filename(node.GetName(), 'Segmentation')}.seg.nrrd"
            path = os.path.join(seg_folder, filename)
            ok = bool(slicer.util.saveNode(node, path))
            rec = {
                "name": node.GetName(),
                "file": os.path.relpath(path, backup_dir),
                "saved": ok,
                "is_plan_segmentation": node is segmentation_node,
                "attributes": node_attributes(node),
            }
            segmentation_records.append(rec)
            print(f"Segmentation {'OK' if ok else 'FAILED'}: {node.GetName()}")
        if not any(x["saved"] and x["is_plan_segmentation"] for x in segmentation_records):
            raise RuntimeError("Failed to save the plan's active segmentation.")

        # Plan metadata
        segmentation = segmentation_node.GetSegmentation()
        target_ids = [str(x) for x in plan.GetTargetSegmentIDs()]
        targets = []
        for sid in target_ids:
            segment = segmentation.GetSegment(sid)
            targets.append({"id": sid, "name": segment.GetName() if segment else sid})
        body_id = plan.GetBodySegmentID()
        body_segment = segmentation.GetSegment(body_id) if body_id else None
        isocenter = [0.0, 0.0, 0.0]
        plan.GetIsocenterPosition(isocenter)
        output_node = plan.GetOutputTotalDoseVolumeNode()

        plan_record = {
            "name": plan.GetName(),
            "reference_volume_name": reference_node.GetName(),
            "reference_volume_file": reference_filename,
            "segmentation_name": segmentation_node.GetName(),
            "targets": targets,
            "body_segment_id": body_id,
            "body_segment_name": body_segment.GetName() if body_segment else None,
            "isocenter_ras_mm": [float(v) for v in isocenter],
            "isocenter_specification": int(plan.GetIsocenterSpecification()),
            "dose_engine": plan.GetDoseEngineName(),
            "plan_optimizer": plan.GetPlanOptimizerName(),
            "inverse_plan": bool(plan.GetInversePlanFlag()),
            "ion_plan": bool(plan.GetIonPlanFlag()),
            "rx_dose_Gy": float(plan.GetRxDose()),
            "dose_grid_spacing_mm": grid_state.get("effective_spacing_mm"),
            "dose_grid": grid_state,
            "output_total_dose_name": output_node.GetName() if output_node else None,
            "attributes": node_attributes(plan),
        }

        # Beams
        beam_records = []
        print("\nBeams:")
        for beam in get_plan_beams(plan):
            try:
                dim = [int(v) for v in beam.GetDoseGridDim()]
            except Exception:
                dim = None
            try:
                spacing = _grid_list(beam.GetDoseGridSpacing())
            except Exception:
                spacing = None
            try:
                nnz = int(beam.GetDoseInfluenceMatrixNumberOfNonZeroElements())
            except Exception:
                nnz = 0
            record = {
                "name": beam.GetName(),
                "beam_number": int(beam.GetBeamNumber()),
                "description": beam.GetBeamDescription() or "",
                "gantry_angle_deg": float(beam.GetGantryAngle()),
                "couch_angle_deg": float(beam.GetCouchAngle()),
                "collimator_angle_deg": float(beam.GetCollimatorAngle()),
                "weight": float(beam.GetBeamWeight()),
                "energy": float(beam.GetBeamEnergy()),
                "SAD_mm": float(beam.GetSAD()),
                "jaws_mm": {
                    "X1": float(beam.GetX1Jaw()), "X2": float(beam.GetX2Jaw()),
                    "Y1": float(beam.GetY1Jaw()), "Y2": float(beam.GetY2Jaw()),
                },
                "source_to_jaws_X_mm": float(beam.GetSourceToJawsDistanceX()),
                "source_to_jaws_Y_mm": float(beam.GetSourceToJawsDistanceY()),
                "source_to_MLC_mm": float(beam.GetSourceToMultiLeafCollimatorDistance()),
                "dose_grid_dim": dim,
                "dose_grid_spacing_mm": spacing,
                "dij_nnz": nnz,
                "attributes": node_attributes(beam),
                "dij_file": None,
            }
            if save_dij and nnz > 0:
                try:
                    record["dij_file"] = save_beam_dij(beam, backup_dir)
                except Exception as exc:
                    print(f"WARNING: DIJ save failed for {beam.GetName()}: {exc}")
            beam_records.append(record)
            radiation_mode = record["attributes"].get("pyRadPlan.radiationMode")
            print(
                f"  {beam.GetName()}: gantry={beam.GetGantryAngle():.1f}, "
                f"couch={beam.GetCouchAngle():.1f}, weight={beam.GetBeamWeight():.3f}, "
                f"radiationMode={radiation_mode}, grid={spacing}, "
                f"DIJ={'yes' if record['dij_file'] else 'no'}"
            )

        # Objectives
        objective_rows = save_objective_table(plan, ebp)
        print("\nObjectives:")
        for i, obj in enumerate(objective_rows, start=1):
            print(
                f"  {i:2d}. {obj['name']} | {obj['segment_name']} | "
                f"OP={obj['overlap_priority']} | p={obj['penalty']} | {obj['parameters']}"
            )
        if plan.GetInversePlanFlag() and len(objective_rows) == 0:
            print("WARNING: inverse plan has zero objective rows in the current Objectives table.")

        optimizer_ui_settings = collect_optimizer_ui_settings(ebp)

        # Dose-like volumes
        dose_records = []
        skipped_empty = []
        failed_dose_saves = []
        if save_dose_volumes:
            dose_folder = os.path.join(backup_dir, "DoseVolumes")
            os.makedirs(dose_folder, exist_ok=True)
            plan_name_l = (plan.GetName() or "").lower()
            beam_names_l = [(b.GetName() or "").lower() for b in get_plan_beams(plan)]
            output_id = output_node.GetID() if output_node else None
            for node in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode"):
                if not looks_like_dose_volume(node):
                    continue
                if str(dose_scope).lower() == "related":
                    name_l = (node.GetName() or "").lower()
                    related = (
                        (output_id and node.GetID() == output_id)
                        or (plan_name_l and plan_name_l in name_l)
                        or any(bn and bn in name_l for bn in beam_names_l)
                    )
                    if not related:
                        continue
                valid, meta = is_valid_scalar_volume(node)
                if not valid:
                    skipped_empty.append(node.GetName())
                    print("Skipping EMPTY/INVALID dose-like node:", node.GetName())
                    continue
                base = safe_filename(node.GetName(), "Dose")
                nrrd_name = f"{base}.nrrd"
                nrrd_path = os.path.join(dose_folder, nrrd_name)
                ok = False
                try:
                    ok = bool(slicer.util.saveNode(node, nrrd_path))
                except Exception as exc:
                    print(f"WARNING: NRRD dose save exception for {node.GetName()}: {exc}")
                    ok = False
                record = {
                    "name": node.GetName(),
                    "file": os.path.relpath(nrrd_path, backup_dir) if ok else None,
                    "fallback_npz_file": None,
                    "range": meta["range"],
                    "dimensions": meta["dimensions"],
                    "components": meta["components"],
                    "attributes": node_attributes(node),
                    "is_plan_output": bool(output_id and node.GetID() == output_id),
                }
                if not ok and dose_numpy_fallback:
                    try:
                        fallback_name = f"{base}_volume.npz"
                        fallback_path = os.path.join(dose_folder, fallback_name)
                        save_volume_numpy_fallback(node, fallback_path)
                        record["fallback_npz_file"] = os.path.relpath(fallback_path, backup_dir)
                        ok = True
                        print("Dose saved with NPZ fallback:", node.GetName())
                    except Exception as exc:
                        print(f"WARNING: NPZ fallback failed for {node.GetName()}: {exc}")
                if ok:
                    dose_records.append(record)
                else:
                    failed_dose_saves.append(node.GetName())
                    print("WARNING: failed to save dose volume:", node.GetName())

        backup = {
            "schema_version": BACKUP_SCHEMA_VERSION,
            "script_version": SCRIPT_VERSION,
            "created": datetime.now().isoformat(timespec="seconds"),
            "slicer_version": slicer.app.applicationVersion,
            "plan": plan_record,
            "segmentations": segmentation_records,
            "beams": beam_records,
            "objectives": objective_rows,
            "optimizer_ui_settings": optimizer_ui_settings,
            "dose_volumes": dose_records,
            "skipped_empty_dose_nodes": skipped_empty,
            "failed_dose_saves": failed_dose_saves,
            "backup_options": {
                "save_dij": bool(save_dij),
                "save_dose_volumes": bool(save_dose_volumes),
                "dose_scope": str(dose_scope),
                "dose_numpy_fallback": bool(dose_numpy_fallback),
            },
        }

        backup_json = os.path.join(backup_dir, BACKUP_FILE)
        with open(backup_json, "w", encoding="utf-8") as f:
            json.dump(backup, f, indent=2)
        with open(os.path.join(backup_dir, COMPATIBILITY_FILE), "w", encoding="utf-8") as f:
            json.dump(backup, f, indent=2)

        # Immediate JSON validation before declaring backup complete.
        with open(backup_json, "r", encoding="utf-8") as f:
            check = json.load(f)
        if int(check.get("schema_version", -1)) != BACKUP_SCHEMA_VERSION:
            raise RuntimeError("Backup JSON validation failed: schema version mismatch.")
        if check.get("plan", {}).get("name") != plan.GetName():
            raise RuntimeError("Backup JSON validation failed: plan name mismatch.")
        if len(check.get("beams", [])) != len(beam_records):
            raise RuntimeError("Backup JSON validation failed: beam count mismatch.")
        if len(check.get("objectives", [])) != len(objective_rows):
            raise RuntimeError("Backup JSON validation failed: objective count mismatch.")

        with open(os.path.join(backup_dir, "Backup_summary.txt"), "w", encoding="utf-8") as f:
            f.write(f"RTPlan backup schema: {BACKUP_SCHEMA_VERSION}\n")
            f.write(f"Script version: {SCRIPT_VERSION}\n")
            f.write(f"Created: {backup['created']}\n")
            f.write(f"Plan: {plan.GetName()}\n")
            f.write(f"Ion plan: {plan_record['ion_plan']}\n")
            f.write(f"Reference CT: {reference_node.GetName()}\n")
            f.write(f"Segmentation: {segmentation_node.GetName()}\n")
            f.write(f"Targets: {[x['name'] for x in targets]}\n")
            f.write(f"BODY: {plan_record['body_segment_name']}\n")
            f.write(f"Rx dose Gy: {plan_record['rx_dose_Gy']}\n")
            f.write(
                f"Dose grid effective mm: {grid_state['effective_spacing_mm']} "
                f"(source={grid_state['effective_source']})\n"
            )
            f.write(f"RTPlan grid mm: {grid_state['plan_spacing_mm']}\n")
            f.write(f"EBP UI grid mm: {grid_state['ui_spacing_mm']}\n")
            f.write(f"Beams: {len(beam_records)}\n")
            f.write(f"Objectives: {len(objective_rows)}\n")
            f.write(f"Segmentations: {len(segmentation_records)}\n")
            f.write(f"Dose volumes saved: {len(dose_records)}\n")
            if skipped_empty:
                f.write(f"Skipped empty dose nodes: {skipped_empty}\n")
            if failed_dose_saves:
                f.write(f"Dose volumes that failed export: {failed_dose_saves}\n")
            for warning in grid_state["warnings"]:
                f.write(f"WARNING: {warning}\n")

        with open(os.path.join(backup_dir, BACKUP_COMPLETE_MARKER), "w", encoding="utf-8") as f:
            f.write("Backup completed and primary JSON was successfully reloaded/validated.\n")
        write_status("COMPLETE", "Backup JSON validated.", {
            "plan": plan.GetName(), "beams": len(beam_records),
            "objectives": len(objective_rows),
            "effective_dose_grid_mm": grid_state["effective_spacing_mm"],
        })

        os.rename(backup_dir, final_backup_dir)
        backup_dir = final_backup_dir
        print("\n" + "=" * 78)
        print("BACKUP COMPLETE")
        print("=" * 78)
        print("Folder:", backup_dir)
        print("Plan:", plan.GetName())
        print("Ion plan:", bool(plan.GetIonPlanFlag()))
        print("Dose grid:", grid_state["effective_spacing_mm"], f"(source={grid_state['effective_source']})")
        print("Beams:", len(beam_records))
        print("Objectives:", len(objective_rows))
        print("Segmentations:", len(segmentation_records))
        print("Dose volumes:", len(dose_records))
        if failed_dose_saves:
            print("Dose volumes that failed export:", failed_dose_saves)
        print("=" * 78)
        print("The folder is complete and safe to use for restore.")
        return backup_dir

    except Exception as exc:
        try:
            write_status("FAILED", repr(exc))
        except Exception:
            pass
        print("\n" + "!" * 78)
        print("BACKUP FAILED")
        print("!" * 78)
        print("Incomplete folder kept for diagnostics:", backup_dir)
        print("Reason:", exc)
        print("This folder will NOT appear in the normal backup picker.")
        print("!" * 78)
        raise
    finally:
        # Put the user's previous plan selection back if an explicit different plan
        # was backed up.  For normal plan_name=None use this is a no-op.
        try:
            if active_before is not None and active_before is not plan:
                set_active_plan_in_ebp(active_before, ebp)
        except Exception:
            pass


# ==============================================================================
# Backup discovery / loading / validation
# ==============================================================================

def recognized_backup_json_path(backup_dir):
    candidates = [BACKUP_FILE] + PREVIOUS_BACKUP_FILES + [COMPATIBILITY_FILE]
    for name in candidates:
        path = os.path.join(backup_dir, name)
        if os.path.isfile(path):
            return path
    return None


def list_backup_folders(parent_dir):
    parent_dir = normalize_path(parent_dir)
    if not os.path.isdir(parent_dir):
        return []
    search_dirs = [parent_dir]
    for name in os.listdir(parent_dir):
        full = os.path.join(parent_dir, name)
        if os.path.isdir(full) and re.fullmatch(r"\d{8}", name):
            search_dirs.append(full)
    result = []
    for folder in search_dirs:
        try:
            names = os.listdir(folder)
        except Exception:
            continue
        for name in names:
            full = os.path.join(folder, name)
            if not os.path.isdir(full) or name.endswith("_INCOMPLETE"):
                continue
            if not BACKUP_FOLDER_REGEX.match(name):
                continue
            json_path = recognized_backup_json_path(full)
            if not json_path:
                continue
            # v5 native requires a completion marker. Older formats remain selectable.
            if os.path.basename(json_path) == BACKUP_FILE:
                if not os.path.isfile(os.path.join(full, BACKUP_COMPLETE_MARKER)):
                    continue
            result.append(full)
    result.sort(reverse=True)
    return result


def choose_backup_from_parent(parent_dir):
    backups = list_backup_folders(parent_dir)
    if not backups:
        raise RuntimeError(f"No complete RTPlan backup folders found below:\n{parent_dir}")
    labels = [os.path.relpath(x, normalize_path(parent_dir)) for x in backups]
    selection, ok = qinput_get_item("Choose RTPlan backup", "Backup:", labels, 0, False)
    if not ok or not selection:
        return None
    idx = labels.index(selection)
    return backups[idx]


def _legacy_convert(old, backup_dir):
    """Convert the original pre-v3 Plan_summary structure into v5-shaped data."""
    if "Plan" not in old:
        return None
    plan_old = old["Plan"]
    target_names = plan_old.get("target_segments") or []
    if not target_names and plan_old.get("target_segment"):
        target_names = [plan_old["target_segment"]]
    objectives = []
    for obj in old.get("objectives", []):
        params = dict(obj.get("parameters", {}))
        is_constraint = str(params.pop("isConstraint", "false")).lower() == "true"
        objectives.append({
            "is_constraint": is_constraint,
            "name": obj.get("name", ""),
            "segment_name": obj.get("segment_name", ""),
            "segment_id": obj.get("segment_id", ""),
            "overlap_priority": int(obj.get("overlap_priority") or 0),
            "penalty": int(obj.get("penalty") or 0),
            "parameters": params,
        })
    converted = {
        "schema_version": BACKUP_SCHEMA_VERSION,
        "created": "converted-from-legacy",
        "slicer_version": "unknown",
        "plan": {
            "name": plan_old.get("name", "RTPlan"),
            "reference_volume_name": plan_old.get("reference_volume"),
            "reference_volume_file": None,
            "segmentation_name": plan_old.get("segmentation"),
            "targets": [{"id": None, "name": n} for n in target_names],
            "body_segment_id": None,
            "body_segment_name": "BODY",
            "isocenter_ras_mm": plan_old.get("isocenter_ras", [0, 0, 0]),
            "isocenter_specification": 1,
            "dose_engine": plan_old.get("dose_engine", "pyRadPlan"),
            "plan_optimizer": plan_old.get("plan_optimizer", "pyRadPlan"),
            "inverse_plan": bool(plan_old.get("is_inverse", True)),
            "ion_plan": bool(plan_old.get("ion_plan", False)),
            "rx_dose_Gy": float(plan_old.get("rx_dose_Gy", 70.0)),
            "dose_grid_spacing_mm": None,
            "dose_grid": {"effective_spacing_mm": None, "effective_source": "legacy"},
            "output_total_dose_name": None,
            "attributes": {},
        },
        "segmentations": [],
        "beams": [],
        "objectives": objectives,
        "optimizer_ui_settings": {},
        "dose_volumes": [],
    }
    for b in old.get("Beams", []):
        dij_path = b.get("dij_path")
        converted["beams"].append({
            "name": b.get("name", "Beam"),
            "beam_number": -1,
            "description": "",
            "gantry_angle_deg": float(b.get("gantry_angle", 0)),
            "couch_angle_deg": float(b.get("couch_angle", 0)),
            "collimator_angle_deg": 0.0,
            "weight": 1.0,
            "energy": -1.0,
            "SAD_mm": 2000.0,
            "jaws_mm": {"X1": -100.0, "X2": 100.0, "Y1": -100.0, "Y2": 100.0},
            "source_to_jaws_X_mm": 500.0,
            "source_to_jaws_Y_mm": 500.0,
            "source_to_MLC_mm": 400.0,
            "dose_grid_dim": b.get("dose_grid_dim"),
            "dose_grid_spacing_mm": b.get("dose_grid_spacing"),
            "dij_nnz": None,
            "attributes": {},
            "dij_file": os.path.basename(dij_path) if dij_path else None,
        })
    return converted


def load_backup_json(backup_dir):
    backup_dir = normalize_path(backup_dir)
    path = recognized_backup_json_path(backup_dir)
    if path is None:
        raise RuntimeError(
            f"No recognized backup JSON ({BACKUP_FILE}, {PREVIOUS_BACKUP_FILES}, {COMPATIBILITY_FILE}) found in:\n{backup_dir}"
        )
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    schema = raw.get("schema_version")
    if schema == BACKUP_SCHEMA_VERSION:
        return raw
    if schema in (4, 3) and "plan" in raw:
        print(f"WARNING: restoring schema-v{schema} backup in v5 compatibility mode.")
        upgraded = dict(raw)
        upgraded["schema_version"] = BACKUP_SCHEMA_VERSION
        plan = dict(upgraded.get("plan", {}))
        grid = plan.get("dose_grid_spacing_mm")
        plan.setdefault("dose_grid", {
            "effective_spacing_mm": grid,
            "effective_source": f"schema_v{schema}_plan",
            "plan_spacing_mm": grid,
            "ui_spacing_mm": None,
            "beam_grids": [],
            "warnings": [],
        })
        plan.setdefault("ion_plan", False)
        upgraded["plan"] = plan
        upgraded.setdefault("optimizer_ui_settings", {})
        upgraded.setdefault("dose_volumes", [])
        return upgraded
    converted = _legacy_convert(raw, backup_dir)
    if converted:
        print("WARNING: restoring legacy Plan_summary.json with limited compatibility.")
        return converted
    raise RuntimeError(f"Unrecognized backup format in:\n{path}")


def validate_backup(backup_dir, verbose=True):
    """READ-ONLY backup validation; does not alter the Slicer scene."""
    backup_dir = normalize_path(backup_dir)
    errors, warnings = [], []
    try:
        data = load_backup_json(backup_dir)
    except Exception as exc:
        data = None
        errors.append(str(exc))

    if verbose:
        print("\n" + "=" * 78)
        print("READ-ONLY RTPLAN BACKUP VALIDATION v5")
        print("=" * 78)
        print("Folder:", backup_dir)

    if data:
        plan = data.get("plan", {})
        if verbose:
            print("Plan:", plan.get("name"))
            print("Ion plan:", plan.get("ion_plan"))
            print("Rx dose:", plan.get("rx_dose_Gy"))
            print("Dose grid:", (plan.get("dose_grid") or {}).get("effective_spacing_mm") or plan.get("dose_grid_spacing_mm"))

        # CT
        ref = plan.get("reference_volume_file")
        ref_path = resolve_file(backup_dir, ref) if ref else None
        if ref_path is None:
            candidates = [os.path.join(backup_dir, x) for x in os.listdir(backup_dir) if x.startswith("ReferenceVolume_") and x.lower().endswith(".nrrd")]
            ref_path = candidates[0] if candidates else None
        if not ref_path or not os.path.isfile(ref_path) or os.path.getsize(ref_path) <= 0:
            errors.append("Reference CT file missing/empty.")

        # Active segmentation
        active_seg_records = [x for x in data.get("segmentations", []) if x.get("is_plan_segmentation")]
        if active_seg_records:
            for rec in active_seg_records:
                p = resolve_file(backup_dir, rec.get("file"))
                if not p or not os.path.isfile(p) or os.path.getsize(p) <= 0:
                    errors.append(f"Active segmentation missing/empty: {rec.get('name')}")
        elif data.get("segmentations"):
            warnings.append("No segmentation record marked is_plan_segmentation=True.")
        else:
            seg_dir = os.path.join(backup_dir, "Segmentations")
            if not os.path.isdir(seg_dir) or not any(x.lower().endswith(".seg.nrrd") for x in os.listdir(seg_dir)):
                errors.append("No saved segmentation found.")

        # Beams / DIJs
        for beam in data.get("beams", []):
            rel = beam.get("dij_file")
            if rel:
                p = resolve_file(backup_dir, rel)
                if not p or not os.path.isfile(p) or os.path.getsize(p) <= 0:
                    errors.append(f"DIJ missing/empty: {beam.get('name')}")
                elif not zipfile.is_zipfile(p):
                    errors.append(f"DIJ NPZ is not a valid ZIP/NPZ: {beam.get('name')}")
            elif beam.get("dij_nnz", 0):
                warnings.append(f"Beam had a DIJ but no saved DIJ file: {beam.get('name')}")

        # Dose volume files recorded
        for dose in data.get("dose_volumes", []):
            p = resolve_file(backup_dir, dose.get("file")) if dose.get("file") else None
            f = resolve_file(backup_dir, dose.get("fallback_npz_file")) if dose.get("fallback_npz_file") else None
            if not ((p and os.path.isfile(p) and os.path.getsize(p) > 0) or (f and os.path.isfile(f) and os.path.getsize(f) > 0)):
                errors.append(f"Saved dose record has no existing file: {dose.get('name')}")

        # Native v5 completion marker
        if os.path.isfile(os.path.join(backup_dir, BACKUP_FILE)) and not os.path.isfile(os.path.join(backup_dir, BACKUP_COMPLETE_MARKER)):
            errors.append("Native v5 backup lacks BACKUP_COMPLETE.txt.")

    passed = not errors
    if verbose:
        print("Beams:", len(data.get("beams", [])) if data else 0)
        print("Objectives:", len(data.get("objectives", [])) if data else 0)
        if warnings:
            print("\nWarnings:")
            for x in warnings:
                print(" -", x)
        if errors:
            print("\nErrors:")
            for x in errors:
                print(" -", x)
        print("\nRESULT:", "PASS" if passed else "FAIL")
        print("No nodes were loaded, removed, or modified by this validation.")
        print("=" * 78)
    return {"passed": passed, "errors": errors, "warnings": warnings, "data": data}


# ==============================================================================
# Restore helpers
# ==============================================================================

def _load_volume(path):
    result = slicer.util.loadVolume(path)
    if isinstance(result, tuple):
        return result[-1]
    return result


def _load_segmentation(path):
    result = slicer.util.loadSegmentation(path)
    if isinstance(result, tuple):
        return result[-1]
    return result


def find_segment_id_by_name(segmentation_node, name):
    if not segmentation_node or not name:
        return None
    seg = segmentation_node.GetSegmentation()
    try:
        ids = [str(x) for x in seg.GetSegmentIDs()]
    except Exception:
        ids_array = vtk.vtkStringArray()
        seg.GetSegmentIDs(ids_array)
        ids = [ids_array.GetValue(i) for i in range(ids_array.GetNumberOfValues())]
    for sid in ids:
        segment = seg.GetSegment(sid)
        if segment and segment.GetName() == str(name):
            return sid
    return None


def resolve_saved_segment_id(segmentation_node, saved_id, saved_name):
    if saved_id:
        try:
            if segmentation_node.GetSegmentation().GetSegment(str(saved_id)) is not None:
                return str(saved_id)
        except Exception:
            pass
    return find_segment_id_by_name(segmentation_node, saved_name)


def _unique_node_name(base):
    if not slicer.mrmlScene.GetFirstNodeByName(base):
        return base
    i = 1
    while slicer.mrmlScene.GetFirstNodeByName(f"{base}_RESTORED_{i}"):
        i += 1
    return f"{base}_RESTORED_{i}"


def _apply_beam_record(beam, record):
    setters = [
        ("SetName", record.get("name")),
        ("SetBeamNumber", record.get("beam_number")),
        ("SetBeamDescription", record.get("description")),
        ("SetGantryAngle", record.get("gantry_angle_deg")),
        ("SetCouchAngle", record.get("couch_angle_deg")),
        ("SetCollimatorAngle", record.get("collimator_angle_deg")),
        ("SetBeamWeight", record.get("weight")),
        ("SetBeamEnergy", record.get("energy")),
        ("SetSAD", record.get("SAD_mm")),
        ("SetSourceToJawsDistanceX", record.get("source_to_jaws_X_mm")),
        ("SetSourceToJawsDistanceY", record.get("source_to_jaws_Y_mm")),
        ("SetSourceToMultiLeafCollimatorDistance", record.get("source_to_MLC_mm")),
    ]
    for method_name, value in setters:
        if value is None or not hasattr(beam, method_name):
            continue
        try:
            getattr(beam, method_name)(value)
        except Exception:
            try:
                getattr(beam, method_name)(str(value))
            except Exception:
                pass
    jaws = record.get("jaws_mm") or {}
    for key, method_name in (("X1", "SetX1Jaw"), ("X2", "SetX2Jaw"), ("Y1", "SetY1Jaw"), ("Y2", "SetY2Jaw")):
        if key in jaws and hasattr(beam, method_name):
            try:
                getattr(beam, method_name)(float(jaws[key]))
            except Exception:
                pass
    restore_node_attributes(beam, record.get("attributes"))
    # Explicitly re-apply pyRadPlan radiation mode through the scripted engine when available.
    rad_mode = (record.get("attributes") or {}).get("pyRadPlan.radiationMode")
    if rad_mode is not None:
        try:
            slicer.pyRadPlanEngine.scriptedEngine.setParameter(beam, "radiationMode", int(rad_mode))
        except Exception:
            try:
                beam.SetAttribute("pyRadPlan.radiationMode", str(rad_mode))
            except Exception:
                pass


def restore_backup(
    backup_dir,
    clear_scene=False,
    restore_dij=True,
    restore_dose_volumes=True,
    restore_all_segmentations=True,
    plan_name_override=None,
):
    """
    Restore a backup into the current scene.

    clear_scene=False is intentionally safe: the current scene is not deleted.
    If a plan with the same name already exists, a _RESTORED_N name is generated.
    """
    backup_dir = normalize_path(backup_dir)
    data = load_backup_json(backup_dir)
    plan_data = data.get("plan", {})

    print("\n" + "=" * 78)
    print("RTPLAN RESTORE v5")
    print("=" * 78)
    print("Backup:", backup_dir)
    print("Saved plan:", plan_data.get("name"))

    validation = validate_backup(backup_dir, verbose=False)
    if not validation["passed"]:
        print("WARNING: backup validation reported errors:")
        for err in validation["errors"]:
            print(" -", err)
        if not ask_yes_no("Backup validation warning", "The selected backup has validation errors. Continue restore anyway?", False):
            print("Restore cancelled.")
            return None

    if clear_scene:
        if not ask_yes_no("Clear Slicer scene", "This will remove ALL nodes currently in the Slicer scene before restore. Continue?", False):
            print("Restore cancelled.")
            return None
        slicer.mrmlScene.Clear(0)
        process_events(5)

    ebp = get_external_beam_planning_widget(select_module=True)

    # Reference CT
    reference_file = plan_data.get("reference_volume_file")
    reference_path = resolve_file(backup_dir, reference_file) if reference_file else None
    if not reference_path or not os.path.isfile(reference_path):
        candidates = [os.path.join(backup_dir, x) for x in os.listdir(backup_dir) if x.startswith("ReferenceVolume_") and x.lower().endswith(".nrrd")]
        reference_path = candidates[0] if candidates else None
    if not reference_path:
        raise RuntimeError("Could not locate saved reference CT.")
    print("Loading CT:", reference_path)
    reference_node = _load_volume(reference_path)
    if reference_node is None:
        raise RuntimeError("Failed to load reference CT.")
    reference_node.SetName(_unique_node_name(plan_data.get("reference_volume_name") or reference_node.GetName()))

    # Segmentations
    segmentation_records = data.get("segmentations", [])
    plan_seg_record = next((x for x in segmentation_records if x.get("is_plan_segmentation")), None)
    loaded_segmentations = []
    if segmentation_records:
        records_to_load = segmentation_records if restore_all_segmentations else ([plan_seg_record] if plan_seg_record else [segmentation_records[0]])
        for rec in records_to_load:
            if not rec or not rec.get("saved", True):
                continue
            path = resolve_file(backup_dir, rec.get("file"))
            if not path or not os.path.isfile(path):
                print("WARNING: segmentation file missing:", rec.get("name"))
                continue
            print("Loading segmentation:", path)
            node = _load_segmentation(path)
            if node:
                node.SetName(_unique_node_name(rec.get("name") or node.GetName()))
                restore_node_attributes(node, rec.get("attributes"))
                loaded_segmentations.append((rec, node))
    else:
        # Legacy folder fallback.
        seg_dir = os.path.join(backup_dir, "Segmentations")
        if os.path.isdir(seg_dir):
            files = sorted(x for x in os.listdir(seg_dir) if x.lower().endswith(".seg.nrrd"))
            for filename in files[:1 if not restore_all_segmentations else len(files)]:
                path = os.path.join(seg_dir, filename)
                node = _load_segmentation(path)
                if node:
                    loaded_segmentations.append(({"name": node.GetName(), "is_plan_segmentation": not loaded_segmentations}, node))
    if not loaded_segmentations:
        raise RuntimeError("No segmentation could be loaded from backup.")

    segmentation_node = None
    if plan_seg_record:
        for rec, node in loaded_segmentations:
            if rec is plan_seg_record or rec.get("name") == plan_seg_record.get("name"):
                segmentation_node = node
                break
    if segmentation_node is None:
        wanted_name = plan_data.get("segmentation_name")
        for rec, node in loaded_segmentations:
            if rec.get("name") == wanted_name:
                segmentation_node = node
                break
    segmentation_node = segmentation_node or loaded_segmentations[0][1]

    # Plan node - do not overwrite an existing plan by default.
    saved_plan_name = plan_name_override or plan_data.get("name") or "RTPlan"
    restored_plan_name = _unique_node_name(saved_plan_name)
    plan = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLRTPlanNode", restored_plan_name)
    plan.SetAndObserveReferenceVolumeNode(reference_node)
    plan.SetAndObserveSegmentationNode(segmentation_node)
    restore_node_attributes(plan, plan_data.get("attributes"))

    # Put plan in same study as CT if possible.
    try:
        sh = slicer.mrmlScene.GetSubjectHierarchyNode()
        ref_item = sh.GetItemByDataNode(reference_node)
        plan_item = sh.GetItemByDataNode(plan)
        parent = sh.GetItemParent(ref_item)
        if parent:
            sh.SetItemParent(plan_item, parent)
    except Exception:
        pass

    # Core plan settings using direct current RTPlan API.
    plan.SetIonPlanFlag(bool(plan_data.get("ion_plan", False)))
    plan.SetInversePlanFlag(bool(plan_data.get("inverse_plan", True)))
    if plan_data.get("dose_engine"):
        plan.SetDoseEngineName(str(plan_data.get("dose_engine")))
    if plan_data.get("plan_optimizer"):
        plan.SetPlanOptimizerName(str(plan_data.get("plan_optimizer")))
    if plan_data.get("rx_dose_Gy") is not None:
        plan.SetRxDose(float(plan_data.get("rx_dose_Gy")))

    # Targets / BODY, resolving IDs by name if IDs changed.
    target_ids = []
    for target in plan_data.get("targets", []):
        sid = resolve_saved_segment_id(segmentation_node, target.get("id"), target.get("name"))
        if sid:
            target_ids.append(sid)
        else:
            print("WARNING: target segment not found:", target.get("name"))
    if target_ids:
        plan.SetTargetSegmentIDs(target_ids)
    body_id = resolve_saved_segment_id(
        segmentation_node, plan_data.get("body_segment_id"), plan_data.get("body_segment_name")
    )
    if body_id:
        plan.SetBodySegmentID(body_id)

    # Dose grid
    saved_grid_state = plan_data.get("dose_grid") or {}
    grid = saved_grid_state.get("effective_spacing_mm") or plan_data.get("dose_grid_spacing_mm")
    if _valid_grid(grid):
        grid = [float(v) for v in grid]
        plan.SetDoseGridSpacing(grid)
    else:
        grid = None

    # Isocenter
    iso_spec = int(plan_data.get("isocenter_specification", 1))
    try:
        plan.SetIsocenterSpecification(iso_spec)
    except Exception:
        pass
    if iso_spec == 0 and target_ids:
        try:
            plan.SetIsocenterToTargetCenter()
        except Exception:
            pass
    else:
        iso = plan_data.get("isocenter_ras_mm")
        if iso and len(iso) == 3:
            plan.SetIsocenterPosition([float(v) for v in iso])

    # Make restored plan active and synchronize visible UI controls.
    set_active_plan_in_ebp(plan, ebp)
    if grid:
        set_ui_dose_grid_spacing(ebp, grid)
        plan.SetDoseGridSpacing(grid)
    refresh_optimizer_ui(ebp, plan.GetPlanOptimizerName())
    restore_optimizer_ui_settings(ebp, data.get("optimizer_ui_settings"))

    # Beams through current Add Beam UI path so the dose engine initializes defaults.
    add_beam_button = ebp.findChild(qt.QPushButton, "pushButton_AddBeam")
    if add_beam_button is None:
        raise RuntimeError("Could not find Add beam button in External Beam Planning.")
    print("\nRestoring beams...")
    restored_beams = []
    for rec in data.get("beams", []):
        before_ids = {b.GetID() for b in get_plan_beams(plan)}
        add_beam_button.click()
        process_events(3)
        new_beams = [b for b in get_plan_beams(plan) if b.GetID() not in before_ids]
        if not new_beams:
            raise RuntimeError(f"Failed to create beam '{rec.get('name')}'.")
        beam = new_beams[-1]
        _apply_beam_record(beam, rec)
        restored_dij = False
        if restore_dij and rec.get("dij_file"):
            try:
                restored_dij = load_beam_dij(beam, backup_dir, rec)
            except Exception as exc:
                print(f"WARNING: DIJ restore failed for {beam.GetName()}: {exc}")
        restored_beams.append(beam)
        print(
            f"  {beam.GetName()}: gantry={beam.GetGantryAngle():.1f}, couch={beam.GetCouchAngle():.1f}, "
            f"weight={beam.GetBeamWeight():.3f}, radiationMode={beam.GetAttribute('pyRadPlan.radiationMode')}, "
            f"DIJ={'yes' if restored_dij else 'no'}"
        )

    if grid:
        plan.SetDoseGridSpacing(grid)

    # Objectives
    print("\nRestoring objectives...")
    objective_count = restore_objective_table(plan, ebp, data.get("objectives", []))

    # Dose volumes
    restored_doses = []
    if restore_dose_volumes:
        print("\nRestoring saved dose volumes...")
        for rec in data.get("dose_volumes", []):
            node = None
            nrrd = resolve_file(backup_dir, rec.get("file")) if rec.get("file") else None
            fallback = resolve_file(backup_dir, rec.get("fallback_npz_file")) if rec.get("fallback_npz_file") else None
            try:
                if nrrd and os.path.isfile(nrrd):
                    node = _load_volume(nrrd)
                    if node:
                        node.SetName(_unique_node_name(rec.get("name") or node.GetName()))
                        restore_node_attributes(node, rec.get("attributes"))
                elif fallback and os.path.isfile(fallback):
                    node = load_volume_numpy_fallback(
                        fallback, _unique_node_name(rec.get("name") or "Dose"), rec.get("attributes")
                    )
            except Exception as exc:
                print(f"WARNING: failed to restore dose '{rec.get('name')}': {exc}")
            if node:
                restored_doses.append((rec, node))
                print("  Restored:", node.GetName())

        # Restore output total dose reference when possible.
        output_name = plan_data.get("output_total_dose_name")
        chosen = None
        for rec, node in restored_doses:
            if rec.get("is_plan_output") or (output_name and rec.get("name") == output_name):
                chosen = node
                break
        if chosen:
            try:
                plan.SetAndObserveOutputTotalDoseVolumeNode(chosen)
            except Exception:
                pass

    print("\n" + "=" * 78)
    print("RESTORE COMPLETE")
    print("=" * 78)
    print("Plan:", plan.GetName())
    print("Saved plan name:", plan_data.get("name"))
    print("Ion plan:", bool(plan.GetIonPlanFlag()))
    print("Beams:", len(restored_beams))
    print("Objectives:", objective_count)
    print("Targets:", [t.get("name") for t in plan_data.get("targets", [])])
    print("BODY:", plan_data.get("body_segment_name"))
    print("Dose engine:", plan.GetDoseEngineName())
    print("Optimizer:", plan.GetPlanOptimizerName())
    print("Rx dose:", plan.GetRxDose())
    print("Dose grid:", grid)
    print("DIJ restore:", "requested" if restore_dij else "skipped by option")
    print("=" * 78)
    print("Inspect beam radiation modes/angles and objective rows visually before calculation.")
    return plan


def restore_from_parent(parent_dir=None, **restore_options):
    if parent_dir is None:
        parent_dir = choose_directory("Choose parent folder containing RTPlan backups", DEFAULT_PARENT_DIR)
        if not parent_dir:
            print("Restore cancelled.")
            return None
    backup_dir = choose_backup_from_parent(parent_dir)
    if not backup_dir:
        print("Restore cancelled.")
        return None
    return restore_backup(backup_dir, **restore_options)


# ==============================================================================
# Convenience / status
# ==============================================================================

def help_v5():
    print(r"""
RTPlan Backup / Restore v5 - quick usage
========================================

1) NORMAL: load script + interactive menu
-----------------------------------------
exec(open(r"C:\path\RTPlan_Backup_Restore_v5_Slicer_5_12_3.py").read())

2) LOAD FUNCTIONS ONLY (no menu)
--------------------------------
RTPLAN_V5_NO_MENU = True
exec(open(r"C:\path\RTPlan_Backup_Restore_v5_Slicer_5_12_3.py").read())

3) BACKUP ACTIVE PLAN (currently selected in External Beam Planning)
-------------------------------------------------------------------
backup_current_plan()

4) BACKUP ACTIVE PLAN, fixed parent directory (no folder dialog)
----------------------------------------------------------------
backup_current_plan(
    parent_dir=r"C:\...\Backups"
)

5) BACKUP A SPECIFIC NAMED PLAN
-------------------------------
backup_current_plan(
    parent_dir=r"C:\...\Backups",
    plan_name="RTPlan_Proton_3beam"
)

6) BACKUP OPTIONS
-----------------
backup_current_plan(
    parent_dir=r"C:\...\Backups",
    plan_name=None,              # None = currently selected plan
    save_dij=True,
    save_dose_volumes=True,
    use_daily_subfolder=True,
    dose_scope="all",           # "all" or "related"
    dose_numpy_fallback=False    # True = NPZ fallback if NRRD export fails
)

7) READ-ONLY VALIDATION (does NOT alter current Slicer scene)
------------------------------------------------------------
validate_backup(
    r"C:\...\Backups\20260908\RTPlan_Proton_3beam_20260908_193000"
)

8) RESTORE ONE EXACT BACKUP
---------------------------
restore_backup(
    r"C:\...\Backups\20260908\RTPlan_Proton_3beam_20260908_193000"
)

9) RESTORE OPTIONS
------------------
restore_backup(
    backup_dir=r"C:\...\backup-folder",
    clear_scene=False,           # safest default
    restore_dij=True,
    restore_dose_volumes=True,
    restore_all_segmentations=True,
    plan_name_override=None
)

10) CHOOSE A BACKUP FROM A PARENT DIRECTORY
-------------------------------------------
restore_from_parent(
    parent_dir=r"C:\...\Backups"
)

11) CHECK WHICH PLAN WILL BE USED
---------------------------------
print_active_plan_summary()
""")


def print_active_plan_summary():
    plan = find_plan(None)
    grid = collect_dose_grid_state(plan, get_external_beam_planning_widget(False))
    print("\nACTIVE RT PLAN")
    print("Plan:", plan.GetName())
    print("Ion plan:", bool(plan.GetIonPlanFlag()))
    print("Inverse plan:", bool(plan.GetInversePlanFlag()))
    print("Dose engine:", plan.GetDoseEngineName())
    print("Optimizer:", plan.GetPlanOptimizerName())
    print("Rx Gy:", plan.GetRxDose())
    print("Targets:", list(plan.GetTargetSegmentIDs()))
    print("BODY ID:", plan.GetBodySegmentID())
    print("Dose grid:", grid.get("effective_spacing_mm"), "source=", grid.get("effective_source"))
    print("Beams:")
    for b in get_plan_beams(plan):
        print(
            f"  {b.GetName()}: gantry={b.GetGantryAngle():.1f}, couch={b.GetCouchAngle():.1f}, "
            f"radiationMode={b.GetAttribute('pyRadPlan.radiationMode')}, "
            f"DIJ nnz={b.GetDoseInfluenceMatrixNumberOfNonZeroElements()}"
        )
    return plan


# ==============================================================================
# Interactive menu
# ==============================================================================

def interactive_menu():
    options = [
        "Backup currently selected RT plan",
        "Backup a named RT plan",
        "Restore from a specific backup folder",
        "Choose backup from parent folder and restore",
        "Validate a backup (read-only)",
        "Print active RT plan summary",
        "Print usage help",
        "Cancel",
    ]
    selection, ok = qinput_get_item("RTPlan Backup / Restore v5", "Action:", options, 0, False)
    if not ok or not selection or selection == "Cancel":
        print("Cancelled.")
        return None

    if selection == options[0]:
        return backup_current_plan()

    if selection == options[1]:
        plans = list_rt_plans()
        if not plans:
            raise RuntimeError("No RT plans exist in the scene.")
        names = [p.GetName() for p in plans]
        chosen, ok2 = qinput_get_item("Choose RT plan", "Plan:", names, 0, False)
        if not ok2 or not chosen:
            return None
        return backup_current_plan(plan_name=chosen)

    if selection == options[2]:
        folder = choose_directory("Choose complete RTPlan backup folder", DEFAULT_PARENT_DIR)
        if not folder:
            return None
        return restore_backup(folder)

    if selection == options[3]:
        return restore_from_parent()

    if selection == options[4]:
        folder = choose_directory("Choose RTPlan backup folder to validate", DEFAULT_PARENT_DIR)
        if not folder:
            return None
        return validate_backup(folder, verbose=True)

    if selection == options[5]:
        return print_active_plan_summary()

    if selection == options[6]:
        return help_v5()


# ==============================================================================
# Autorun
# ==============================================================================

print("RTPlan Backup / Restore v5 loaded.")
print("Default behavior: use the RT plan currently selected in External Beam Planning.")

if not globals().get("RTPLAN_V5_NO_MENU", False):
    interactive_menu()
