from __future__ import annotations

from . import legacy

DB = legacy.DB
CITAS = legacy.CITAS
CITAS_DONE = legacy.CITAS_DONE

db = legacy.db
init_db = legacy.init_db
get_lead = legacy.get_lead
upsert_lead = legacy.upsert_lead
list_leads = legacy.list_leads
history = legacy.history
append_history = legacy.append_hist
note = legacy.note
get_setting = legacy.get_setting
set_setting = legacy.set_setting
sync_pool_files = legacy.sync_pool_files
assign_pool = legacy.assign_pool
free_pool_for_lead = legacy.free_pool_for_lead
bind_pool_work_order = legacy.bind_pool_work_order
pool_status = legacy.pool_status
build_agenda = legacy.build_agenda
note_finalizada = legacy.note_finalizada
