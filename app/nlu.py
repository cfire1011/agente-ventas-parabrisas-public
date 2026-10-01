from __future__ import annotations

from . import legacy

client_lang = legacy._client_lang
extract_zip = legacy._extract_zip
extract_phone = legacy._extract_phone
extract_vehicle_from_text = legacy.extract_vehicle_from_text
analyze_client_message = legacy.analyze_client_message
detect_service_pref = legacy.detect_service_pref
parse_when_intent = legacy._parse_when_intent
interpret_slot_request = legacy.interpret_slot_request
looks_like_address = legacy._looks_like_address
looks_like_vehicle_blob = legacy._looks_like_vehicle_blob
client_wants_out = legacy._client_wants_out
client_denies_address = legacy._client_denies_address
client_accepts_taller_manana = legacy._client_accepts_taller_manana
wants_urgency_today = legacy._wants_urgency_today
