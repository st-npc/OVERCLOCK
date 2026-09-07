"""Pluggable demo workloads.

Every task module must expose this contract (no networking/Flask
knowledge — that's what keeps swapping the workload a one-file change):

  DISPLAY_NAME: str
      Human-readable name shown in the dashboard's task picker.

  generate_work(n: int) -> list[str]
      Produce n opaque, self-contained work units as strings (base64 or
      JSON — whatever the task needs). These are what gets split across
      devices; a device never needs to know what's inside one.

  process_batch(items: list[str]) -> dict
      Process a list of work units and return
      {"items": [...], "elapsed_seconds": float, "count": int}. This is
      the function that actually runs the heavy work, both locally and
      inside app_helper.py's /process handler.

  save_result(item: str, out_path_base: str) -> str
      Persist one processed result under `out_path_base` (no extension —
      the task picks its own, e.g. ".png" or ".json") and return the
      actual path used.

Add a new task by dropping a module in this package that implements the
above and registering it in TASKS below — nothing else in the app needs
to change.
"""
from tasks import image_task, render_task

TASKS = {
    "image": image_task,
    "render": render_task,
}
DEFAULT_TASK = "image"


def get_task(task_type: str):
    return TASKS.get(task_type, TASKS[DEFAULT_TASK])


def task_choices() -> list:
    return [{"id": key, "name": getattr(mod, "DISPLAY_NAME", key)} for key, mod in TASKS.items()]
