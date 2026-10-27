import html
import json
import os
import re
import tempfile
import urllib.request
from datetime import datetime

from langflow.custom import Component
from langflow.io import BoolInput, HandleInput, MultilineInput, Output, SecretStrInput
from langflow.schema import Data
from langflow.schema.message import Message


class MCHRowBuilder(Component):
    display_name = "MCH Row Builder"
    description = "Applies the MCH CX Scoring Guide level rules, builds Call Review Tracker rows (formula columns skipped) and optionally pushes them to Excel."
    icon = "table"
    name = "MCHRowBuilder"

    DIMS = [
        ("opening_professional_tone", "Opening & Professional Tone (0-2)"),
        ("active_listening", "Active Listening (0-2)"),
        ("effective_probing", "Effective Probing (0-2)"),
        ("context_understood", "Context Understood (0-2)"),
        ("empathy_acknowledgment", "Empathy / Acknowledgment (0-2)"),
        ("call_control_hold", "Call Control & Hold Management (0-2)"),
        ("summarized_confirmed", "Summarized & Confirmed (0-2)"),
        ("ownership_next_steps", "Ownership & Next Steps (0-2)"),
    ]

    COLUMNS = [
        "Review ID", "Reviewer", "Call Date", "Call Time (ET)", "Interaction / Conversation ID", "Ticket #",
        "Ticket Priority", "Analyst", "Shift", "Caller Role", "Department / Location", "Application / Issue Type",
        "Call Duration (min)", "Queue Wait (sec)", "Repeat Caller / Contact?",
        "If Repeat, Was Previous Contact History Reviewed?", "Outcome", "Assignment Group",
        "Frustration Cue Present?", "Frustration Appropriately Acknowledged?", "Number of Holds",
        "Total Hold Duration (sec)", "Dead Air and Its Duration",
        "Opening & Professional Tone (0-2)", "Active Listening (0-2)", "Effective Probing (0-2)",
        "Context Understood (0-2)", "Empathy / Acknowledgment (0-2)", "Call Control & Hold Management (0-2)",
        "Summarized & Confirmed (0-2)", "Ownership & Next Steps (0-2)", "Total Customer Experience Score",
        "Customer Experience %", "Risk Level", "Key Call Observations", "What Worked Well",
        "Coaching / Corrective Action",
    ]

    PRIORITY_MAP = {"1": "P1 - Critical", "2": "P2 - High", "3": "P3 - Moderate", "4": "P4 - Low"}

    ARTIFACT_RE = r"transcript|transcription|audio|recording|loop|echo|artifact|playback"

    DEFAULT_CONFIG = json.dumps({
        "reviewer": "Faiz",
        "formula_columns": ["Customer Experience %", "Risk Level"],
        "name_aliases": {"sheetal jadhav": "Shital Jadhav", "adersh unni krishnan": "Adersh Unnikrishnan"},
        "shift": {"weekend_days": [5, 6], "start": "08:00", "end": "17:00",
                  "labels": {"business": "Business Hours", "after": "After Hours", "weekend": "Weekends"}},
        "outcome_labels": {"sd": "Resolved by SD", "onsite": "Assigned to MCH onsite team",
                           "gms": "Assigned to Altera GMS team", "disconnected": "User disconnected",
                           "other": "Other"},
        "outcome_groups": {"service_desk": ["MS - Service Desk"], "onsite_prefixes": ["MCH -"],
                           "gms_prefixes": ["GMS -", "MS - AMS"],
                           "not_resolved_phrases": ["3 strike", "three strike"]},
        "rules": {"cap_mode": "objective_only",
                  "interruptions_for_zero": 2, "repeat_requests_for_zero": 2,
                  "name_max_uses": 3, "priority_required_when_routed": True},
        "short_call_sec": 90
    }, indent=1)

    inputs = [
        HandleInput(name="evidence", display_name="Evidence JSON", input_types=["Data"], required=True),
        HandleInput(name="model_response", display_name="CX Scorer Response", input_types=["Message", "Data"], required=True),
        MultilineInput(name="config_json", display_name="Config JSON", value="{}", advanced=True),
        BoolInput(name="store_rows", display_name="Store row in batch file", value=True),
        BoolInput(name="clear_batch", display_name="Clear batch file first", value=False, advanced=True),
        BoolInput(name="push_rows", display_name="Push row to Excel (Power Automate)", value=False),
        SecretStrInput(name="webhook_url", display_name="Power Automate HTTP URL", required=False),
    ]

    outputs = [
        Output(display_name="Paste Block A", name="block_a", method="out_block_a", group_outputs=True),
        Output(display_name="Paste Block B", name="block_b", method="out_block_b", group_outputs=True),
        Output(display_name="Row JSON", name="row_json", method="row_data", group_outputs=True),
        Output(display_name="HTML Report", name="html_report", method="row_html", group_outputs=True),
    ]

    # ---------- helpers ----------
    def _merge(self, base, over):
        for k, v in over.items():
            if isinstance(v, dict) and isinstance(base.get(k), dict) and k != "name_aliases":
                self._merge(base[k], v)
            else:
                base[k] = v
        return base

    def _cfg(self):
        base = json.loads(self.DEFAULT_CONFIG)
        try:
            over = json.loads(self.config_json or "{}")
        except Exception as ex:
            raise ValueError(f"Row Builder Config JSON is not valid JSON: {ex}")
        return self._merge(base, over)

    def _int(self, v):
        try:
            return int(float(v))
        except Exception:
            return None

    def _b(self, v):
        if isinstance(v, bool):
            return v
        s = str(v).strip().lower() if v is not None else ""
        if s in ("true", "yes"):
            return True
        if s in ("false", "no"):
            return False
        return None

    def _ntok(self, s):
        return [t for t in re.sub(r"[^a-z ]", " ", str(s or "").lower()).split() if len(t) > 1]

    def _canon(self, s):
        s = str(s or "").strip()
        return (getattr(self, "_mch_aliases", None) or {}).get(s.lower(), s)

    def _same_person(self, a, b):
        a, b = self._canon(a), self._canon(b)
        ca, cb = re.sub(r"[^a-z]", "", a.lower()), re.sub(r"[^a-z]", "", b.lower())
        if len(ca) >= 6 and ca == cb:
            return True
        ta, tb = set(self._ntok(a)), set(self._ntok(b))
        if len(ta) < 2 or len(tb) < 2:
            return False
        small, big = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
        return small <= big

    def _starts(self, value, prefixes):
        v = str(value or "").strip().lower()
        return bool(v) and any(v.startswith(str(p).lower()) for p in prefixes)

    def _clean(self, v):
        if v is None:
            return ""
        return re.sub(r"[\t\r\n]+", " ", str(v)).strip()

    def _priority(self, v):
        s = str(v or "").strip()
        m = re.match(r"^\s*P?\s*(\d)", s, flags=re.I)
        if m and m.group(1) in self.PRIORITY_MAP:
            return self.PRIORITY_MAP[m.group(1)]
        return s

    def _sec(self, mmss):
        try:
            m, s = str(mmss).split(":")[-2:]
            return int(m) * 60 + int(s)
        except Exception:
            return None

    def _letter(self, idx):
        n, out = idx + 1, ""
        while n:
            n, r = divmod(n - 1, 26)
            out = chr(65 + r) + out
        return out

    def _store_file(self):
        base = os.environ.get("LANGFLOW_CONFIG_DIR") or tempfile.gettempdir()
        d = os.path.join(base, "mch_cx")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, "batch_rows.json")

    def _load_store(self):
        p = self._store_file()
        if self.clear_batch or not os.path.exists(p):
            return {}
        try:
            with open(p, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return {}

    def _save_store(self, store):
        p = self._store_file()
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(store, fh)
        os.replace(tmp, p)

    def _evidence(self):
        e = self.evidence
        if isinstance(e, list):
            e = e[0] if e else None
        d = getattr(e, "data", e)
        if isinstance(d, str):
            d = json.loads(d)
        if not isinstance(d, dict) or "call" not in d:
            raise ValueError("Evidence JSON missing or invalid. Connect MCH Evidence Builder > Evidence JSON.")
        return d

    def _parse_response(self, resp):
        if isinstance(resp, list):
            resp = resp[0] if resp else None
        if resp is None:
            return None, "no response"
        d = getattr(resp, "data", None)
        if isinstance(d, dict) and "scores" in d:
            return d, None
        if isinstance(d, dict):
            txt = d.get("text") or d.get("response") or d.get("result") or json.dumps(d)
        else:
            txt = getattr(resp, "text", None) or str(resp)
        txt = re.sub(r"(?m)^```(?:json)?\s*$", "", str(txt).strip())
        i, j = txt.find("{"), txt.rfind("}")
        if i < 0 or j <= i:
            return None, "no JSON object in response"
        try:
            obj = json.loads(txt[i:j + 1])
        except Exception as ex:
            return None, f"JSON parse error: {ex}"
        if not isinstance(obj, dict) or "scores" not in obj:
            return None, "response has no scores block"
        return obj, None

    def _push(self, row, detail, cfg):
        url = str(self.webhook_url or "").strip()
        if not self.push_rows:
            return "push off (toggle disabled)"
        if not url:
            return "push off (no webhook URL)"
        skip = set(cfg.get("formula_columns") or [])
        payload = {
            "review_id": row["Review ID"],
            "row": {c: row.get(c, "") for c in self.COLUMNS if c not in skip},
            "review_flags": "; ".join(detail["review_reasons"] + detail["notes"]),
            "ai_status": detail.get("ai_status", "AI_DRAFT"),
        }
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                code = r.status
                if code == 202:
                    return "accepted by Power Automate (202)"
                return f"pushed to Excel ({code})"
        except urllib.error.HTTPError as ex:
            body = (ex.read() or b"")[:300].decode("utf-8", "replace")
            msg = f"HTTP {ex.code}: {body}"
            detail["review_reasons"].append(f"excel_push_failed: {msg}")
            return f"push FAILED - {msg}"
        except Exception as ex:
            detail["review_reasons"].append(f"excel_push_failed: {ex}")
            return f"push FAILED - {ex}"

    # ---------- main ----------
    def _build(self):
        if getattr(self, "_mch_row", None) is not None:
            return self._mch_row

        cfg = self._cfg()
        self._mch_aliases = {str(k).lower(): v for k, v in (cfg.get("name_aliases") or {}).items()}
        rules = cfg["rules"]
        ol, og = cfg["outcome_labels"], cfg["outcome_groups"]

        ev = self._evidence()
        call = ev.get("call") or {}
        tk = ev.get("ticket") or {}
        cands = tk.get("candidates") or []
        analyst = call.get("analyst", "")
        review = [f"evidence:{x}" for x in ev.get("flags", [])]
        notes = []

        resp, perr = self._parse_response(self.model_response)
        if perr:
            review.append(f"scorer_output_unusable: {perr}")
            resp = {}
        facts = resp.get("facts") or {}
        unscorable = bool(perr) or self._b(resp.get("transcript_usable")) is False \
            or "evidence:transcript_missing_or_short" in review
        if unscorable:
            review.append("not_scored_transcript_unusable")

        def fb(name):
            return self._b(facts.get(name))

        def fs(name):
            return str(facts.get(name) or "").strip().lower()

        # ---- ticket ----
        sel = tk.get("selected")
        status = str(tk.get("status") or "NOT_FOUND")
        if not sel and cands:
            pick = str((resp.get("ticket") or {}).get("selected") or "").strip().upper()
            if pick and pick not in ("NULL", "NONE"):
                for c in cands:
                    ids = [x.strip().upper() for x in str(c.get("label", "")).split("/")]
                    if pick in ids:
                        sel = c
                        break
                review.append("ticket_selected_by_model" if sel else f"model_ticket_not_in_candidates:{pick}")
        if not sel:
            review.append(f"ticket_{status.lower()}")
        sel = sel or {}

        # ---- outcome ----
        fields = sel.get("fields") or {}
        routed = str(fields.get("routing") or "").strip()
        group = str(sel.get("assignment_group") or "").strip()
        target = routed or group
        state = str(sel.get("state") or "").lower()
        close_notes = str(sel.get("close_notes") or "").lower()
        hint = resp.get("outcome_hint") or {}
        resolved = str(hint.get("resolved_on_call") or "").lower()

        if self._b(hint.get("call_ended_abruptly")) is True and resolved != "yes":
            outcome = ol["disconnected"]
        elif any(p in close_notes for p in og["not_resolved_phrases"]):
            outcome = ol["other"]
            review.append("ticket_closed_under_3_strike_policy")
        elif self._starts(target, og["onsite_prefixes"]):
            outcome = ol["onsite"]
        elif self._starts(target, og["gms_prefixes"]):
            outcome = ol["gms"]
        elif state in ("resolved", "closed") and (self._same_person(sel.get("closed_by"), analyst)
                                                  or self._starts(group, og["service_desk"])):
            outcome = ol["sd"]
        elif resolved == "yes":
            outcome = ol["sd"]
        else:
            outcome = ol["other"]
        routed_away = outcome in (ol["onsite"], ol["gms"])
        if routed and group and routed.lower() != group.lower():
            notes.append(f"routed to {routed}; now with {group}")

        # ---- repeat caller ----
        hist = sel.get("history") or {}
        rep = resp.get("repeat") or {}
        cited = [str(x).upper() for x in hist.get("cited_ticket_numbers") or []]
        prior_nums = [str(x.get("number", "")).upper() for x in hist.get("prior_tickets") or []]
        prt = str(rep.get("prior_related_ticket") or "").strip().upper()
        prt = "" if prt in ("NULL", "NONE") else prt
        fam_ids = [x.strip().upper() for x in str(sel.get("label", "")).split("/")]
        prior_related = bool(prt) and (prt in prior_nums or prt in cited)
        if prt and not prior_related and prt not in fam_ids:
            review.append(f"prior_ticket_not_in_records:{prt}")
        said = self._b(rep.get("caller_said_prior_contact")) is True
        reopened = (self._int(hist.get("reopen_count")) or 0) > 0
        repeat = said or prior_related or bool(cited) or reopened
        hr = str(rep.get("history_reviewed") or "").strip().lower()
        if not repeat:
            hist_rev = "N/A"
        elif hist.get("cited_by_analyst") or hr == "yes":
            hist_rev = "Yes"
        elif hr == "no":
            hist_rev = "No"
        else:
            hist_rev = ""
            review.append("repeat_history_not_determined")

        # ---- measure scores ----
        scores_in = resp.get("scores") or {}
        dims, final = {}, {}
        for key, _col in self.DIMS:
            o = scores_in.get(key) if isinstance(scores_in.get(key), dict) else {}
            raw = o.get("score")
            s = self._int(raw) if raw not in (None, "") else None
            if s not in (0, 1, 2):
                s = None
            evid = [x for x in (o.get("evidence") or []) if isinstance(x, dict) and str(x.get("quote", "")).strip()]
            dims[key] = {"llm": s, "final": s, "rules": [], "rationale": str(o.get("rationale") or ""), "evidence": evid}
            final[key] = s
            if s == 2 and not evid:
                final[key] = 1
                dims[key]["rules"].append("score 2 without evidence")
            if s is None and not unscorable:
                review.append(f"missing_score:{key}")

        strict = str(rules.get("cap_mode", "objective_only")).lower() == "strict"

        def cap(key, limit, why):
            if final.get(key) is not None and final[key] > limit:
                final[key] = limit
                dims[key]["rules"].append(why)

        def soft(key, limit, why):
            if strict:
                cap(key, limit, why)
            elif final.get(key) is not None and final[key] > limit:
                review.append(f"check:{key} ({why})")
                dims[key]["rules"].append(f"flagged, not capped: {why}")

        op, al, pb_, cu = "opening_professional_tone", "active_listening", "effective_probing", "context_understood"
        em, cc, sc, on = "empathy_acknowledgment", "call_control_hold", "summarized_confirmed", "ownership_next_steps"

        # Opening & Professional Tone
        gr, sid, oa = fb("greeting"), fb("self_identified"), fb("offered_assistance")
        if gr is False and sid is False:
            cap(op, 0, "no greeting and no self-identification")
        elif False in (gr, sid, oa):
            soft(op, 1, "an opening element was not evidenced")
        n = self._int(facts.get("caller_name_uses"))
        if n is not None and n > int(rules["name_max_uses"]):
            soft(op, 1, f"caller name used more than {rules['name_max_uses']} times")

        # Active Listening
        ints = self._int(facts.get("agent_interruptions")) or 0
        reps = self._int(facts.get("asked_to_repeat_known_info")) or 0
        if ints >= int(rules["interruptions_for_zero"]):
            cap(al, 0, f"{ints} interruptions")
        elif ints >= 1:
            soft(al, 1, "an interruption was noted")
        if reps >= int(rules["repeat_requests_for_zero"]):
            cap(al, 0, f"asked caller to repeat known information {reps} time(s)")
        elif reps >= 1:
            soft(al, 1, "asked caller to repeat known information once")

        # Effective Probing
        probing = facts.get("probing") if isinstance(facts.get("probing"), dict) else {}
        pr = {k: self._b(v) for k, v in probing.items()}
        if pr.get("before_troubleshooting") is False:
            soft(pb_, 1, "context not gathered before troubleshooting")
        missed = [k for k in ("error_or_symptoms", "affected_users", "work_or_patient_impact",
                              "time_sensitivity", "workaround") if pr.get(k) is False]
        if missed:
            soft(pb_, 1, "not established: " + ", ".join(missed))

        # Context Understood
        summ = fs("summary")
        conf = fb("confirmation_asked")
        if hist_rev == "No":
            cap(cu, 0, "referenced history not reviewed")
        if fb("verification_performed") is False:
            cap(cu, 1, "account action without 2-of-3 verification (KB1568248)")

        # Empathy / Acknowledgment
        cues = [c for c in (facts.get("frustration_cues") or []) if isinstance(c, dict)]
        fack = fs("frustration_acknowledgement")
        if cues and fack == "none":
            cap(em, 0, "frustration, inconvenience or difficulty not recognised")
        elif cues and fack == "generic":
            cap(em, 1, "generic acknowledgement of the caller's situation")

        # Call Control & Hold Management
        holds = self._int(call.get("holds")) or 0
        if holds > 0:
            hp, hrs = fs("hold_permission"), fs("hold_reason")
            if hp == "none":
                cap(cc, 0, "hold without permission")
            elif hp == "some":
                cap(cc, 1, "permission not obtained for every hold")
            if hrs == "none":
                cap(cc, 0, "unexplained hold")
            elif hrs == "some":
                cap(cc, 1, "reason not given for every hold")
            if fb("hold_updates_ok") is False:
                soft(cc, 1, "no update about every two minutes on hold")
            if fb("thanked_on_return") is False:
                soft(cc, 1, "caller not thanked on return")

        # Dead air: code timings reconciled with model reasons
        model_gaps = [g for g in (facts.get("dead_air_gaps") or []) if isinstance(g, dict)]
        code_gaps = [g for g in (ev.get("dead_air_code") or []) if isinstance(g, dict)]
        gaps, unassessed = [], []
        for cg in code_gaps:
            secs = self._int(cg.get("seconds"))
            if not secs or secs <= 30:
                continue
            cs = self._sec(cg.get("start"))
            match = None
            for mg in model_gaps:
                ms = self._sec(mg.get("start"))
                if cs is not None and ms is not None and abs(cs - ms) <= 5:
                    match = mg
                    break
            if match is None:
                unassessed.append(str(cg.get("start", "")))
                continue
            if self._b(match.get("caller_requested")) is True:
                continue
            gaps.append({"start": str(cg.get("start", "")), "seconds": secs,
                         "explained": self._b(match.get("explained")),
                         "updates": str(match.get("updates") or "").lower()})
        if unassessed:
            review.append("dead_air_not_assessed_by_model: " + ", ".join(unassessed))

        for gp in gaps:
            if gp["explained"] is False and gp["updates"] == "none":
                cap(cc, 0, f"unexplained dead air {gp['seconds']}s at {gp['start']} without update")
            elif gp["updates"] in ("none", "partial"):
                cap(cc, 1, f"dead air {gp['seconds']}s at {gp['start']} without regular updates")

        # Summarized & Confirmed
        if summ == "none":
            cap(sc, 0, "no summary")
        elif summ == "partial" or conf is False:
            soft(sc, 1, "partial summary or confirmation not asked")

        # Ownership & Next Steps
        if fb("next_steps_explained") is False:
            cap(on, 0, "no clear next step")
        if sel and fb("ticket_number_given") is False:
            cap(on, 1, "ticket number not given")
        if fb("responsible_team_stated") is False:
            soft(on, 1, "responsible team not stated")
        if fb("expectations_set") is False:
            soft(on, 1, "expectations or timing not set")
        if rules.get("priority_required_when_routed") and routed_away and fb("priority_given") is False:
            soft(on, 1, "priority not given for routed ticket")

        if unscorable:
            for key in final:
                final[key] = None

        for key in final:
            dims[key]["final"] = final[key]
            if dims[key]["llm"] is not None and final[key] is not None and dims[key]["llm"] > final[key]:
                review.append(f"rule_cap:{key}")

        scored = [v for v in final.values() if v is not None]
        total = sum(scored) if len(scored) == 8 else None
        if scored and len(scored) < 8:
            review.append("incomplete_scores_total_left_blank")

        # ---- frustration columns ----
        frus_cues = [c for c in cues if str(c.get("type") or "frustration").lower() == "frustration"]
        if unscorable:
            frus, frus_ack = "", ""
        else:
            frus = "Yes" if frus_cues else "No"
            if not frus_cues:
                frus_ack = "N/A"
            elif fack == "specific":
                frus_ack = "Yes"
            elif fack in ("generic", "none"):
                frus_ack = "No"
            else:
                frus_ack = ""
                review.append("frustration_ack_not_determined")

        # ---- dead air column ----
        disp = list(gaps)
        if unscorable:
            dead = ""
        elif disp:
            longest = max(disp, key=lambda x: self._int(x.get("seconds")) or 0)
            total_da = sum(self._int(x.get("seconds")) or 0 for x in disp)
            if len(disp) == 1:
                dead = f"Yes - {total_da}s @ {longest.get('start', '')}"
            else:
                dead = (f"Yes - {len(disp)} gaps, {total_da}s total "
                        f"(longest {self._int(longest.get('seconds'))}s @ {longest.get('start', '')})")
        else:
            dead = "No"

        # ---- shift ----
        try:
            st = datetime.strptime(call.get("start_et", ""), "%Y-%m-%d %H:%M")
            sh = cfg["shift"]
            if st.weekday() in sh["weekend_days"]:
                shift = sh["labels"]["weekend"]
            elif sh["start"] <= st.strftime("%H:%M") < sh["end"]:
                shift = sh["labels"]["business"]
            else:
                shift = sh["labels"]["after"]
        except Exception:
            shift = ""

        # ---- priority ----
        priority = self._priority(sel.get("priority"))
        if priority and priority not in self.PRIORITY_MAP.values():
            review.append(f"priority_not_in_list:{priority}")
        pci = str(resp.get("patient_care_impact") or "none").lower()
        if not unscorable and pci in ("mentioned", "urgent") and priority in ("P3 - Moderate", "P4 - Low"):
            review.append(f"patient_impact_mentioned_but_ticket_{priority[:2]}")
        if not unscorable and fb("priority_given") is True and priority in ("P3 - Moderate", "P4 - Low"):
            review.append("analyst_promised_priority_check_ticket_priority")

        # ---- review triggers ----
        if (self._int(call.get("duration_sec")) or 0) < int(cfg.get("short_call_sec", 90)):
            review.append("short_call")
        if str(resp.get("confidence") or "").lower() == "low":
            review.append("low_confidence")
        review = list(dict.fromkeys(review))

        # ---- narrative fields ----
        cls = resp.get("classification") or {}
        obs = "Not scored - transcript unavailable." if unscorable else str(resp.get("key_observations") or "")
        if "evidence:non_mch_analyst" in review:
            obs = "[Overflow - non-MCH analyst] " + obs

        def behaviour_only(items):
            if not isinstance(items, list):
                items = [items] if items else []
            keep = [str(x).strip() for x in items
                    if str(x).strip() and not re.search(self.ARTIFACT_RE, str(x), re.I)]
            return "  ".join(f"{i}) {t}" for i, t in enumerate(keep, 1))

        row = {
            "Review ID": ev.get("review_id", ""),
            "Reviewer": cfg.get("reviewer", ""),
            "Call Date": call.get("call_date", ""),
            "Call Time (ET)": call.get("call_time_et", ""),
            "Interaction / Conversation ID": ev.get("conversation_id", ""),
            "Ticket #": sel.get("label", ""),
            "Ticket Priority": priority,
            "Analyst": analyst,
            "Shift": shift,
            "Caller Role": "" if unscorable else (cls.get("caller_role") or ""),
            "Department / Location": "" if unscorable else (cls.get("department_location") or fields.get("department") or fields.get("location") or ""),
            "Application / Issue Type": "" if unscorable else (cls.get("issue_type") or ""),
            "Call Duration (min)": call.get("duration_min", ""),
            "Queue Wait (sec)": call.get("queue_wait_sec", ""),
            "Repeat Caller / Contact?": "Yes" if repeat else "No",
            "If Repeat, Was Previous Contact History Reviewed?": hist_rev,
            "Outcome": outcome,
            "Assignment Group": target,
            "Frustration Cue Present?": frus,
            "Frustration Appropriately Acknowledged?": frus_ack,
            "Number of Holds": holds,
            "Total Hold Duration (sec)": call.get("hold_sec", 0),
            "Dead Air and Its Duration": dead,
        }
        for key, col in self.DIMS:
            row[col] = "" if final[key] is None else final[key]
        row["Total Customer Experience Score"] = "" if total is None else total
        row["Customer Experience %"] = ""
        row["Risk Level"] = ""
        row["Key Call Observations"] = obs
        row["What Worked Well"] = "" if unscorable else behaviour_only(resp.get("what_worked_well"))
        row["Coaching / Corrective Action"] = "" if unscorable else behaviour_only(resp.get("coaching"))
        for c in cfg.get("formula_columns") or []:
            if c in row:
                row[c] = ""

        detail = {"dims": dims, "review_reasons": review, "notes": notes, "total": total,
                  "ticket_status": status, "ai_status": "AI_DRAFT", "confidence": resp.get("confidence"),
                  "issue_summary": cls.get("issue_summary", "")}

        push_state = self._push(row, detail, cfg)

        store = self._load_store()
        if self.store_rows:
            store[row["Review ID"]] = {"row": row, "detail": detail}
            self._save_store(store)

        self.status = f"{'not scored' if total is None else str(total) + '/16'} | {push_state} | review flags: {len(review)}"
        self._mch_row = (row, detail, store, cfg)
        return self._mch_row

    # ---------- paste blocks ----------
    def _blocks(self, cfg):
        fidx = sorted(self.COLUMNS.index(c) for c in (cfg.get("formula_columns") or []) if c in self.COLUMNS)
        if not fidx:
            return list(range(len(self.COLUMNS))), []
        return list(range(0, fidx[0])), list(range(fidx[-1] + 1, len(self.COLUMNS)))

    def _rows(self):
        row, _d, store, cfg = self._build()
        if store:
            items = [it["row"] for it in store.values()]
            items.sort(key=lambda r: (str(r.get("Review ID", ""))[4:12], str(r.get("Call Time (ET)", ""))))
        else:
            items = [row]
        return items, cfg

    def _block_text(self, which):
        rows, cfg = self._rows()
        a, b = self._blocks(cfg)
        idxs = a if which == "A" else b
        if not idxs:
            return "(no columns in this block)"
        lines = ["\t".join(self._clean(r.get(self.COLUMNS[i], "")) for i in idxs) for r in rows]
        self.status = f"{len(rows)} row(s) | paste at column {self._letter(idxs[0])} ({self._letter(idxs[0])}:{self._letter(idxs[-1])})"
        return "\n".join(lines)

    def out_block_a(self) -> Message:
        return Message(text=self._block_text("A"))

    def out_block_b(self) -> Message:
        return Message(text=self._block_text("B"))

    # ---------- other outputs ----------
    def row_data(self) -> Data:
        row, detail, _s, _c = self._build()
        return Data(data={"row": row, "detail": detail})

    def _html(self, row, detail):
        e = html.escape
        skip = {c for _k, c in self.DIMS} | {"Customer Experience %", "Risk Level"}
        drows = ""
        for key, col in self.DIMS:
            d = detail["dims"][key]
            evid = "<br>".join(e(f"[{x.get('ts', '')}] {x.get('quote', '')}") for x in d["evidence"])
            drows += (f"<tr><td>{e(col)}</td><td>{e(str(d['llm'] if d['llm'] is not None else '-'))}</td>"
                      f"<td><b>{e(str(d['final'] if d['final'] is not None else '-'))}</b></td>"
                      f"<td>{e('; '.join(d['rules']))}</td><td>{e(d['rationale'])}</td><td>{evid}</td></tr>")
        info = "".join(f"<tr><th>{e(c)}</th><td>{e(str(row.get(c, '')))}</td></tr>" for c in self.COLUMNS if c not in skip)
        flags = ", ".join(detail["review_reasons"] + detail["notes"]) or "none"
        total = "not scored" if detail["total"] is None else f"{detail['total']} / 16"
        return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{e(row['Review ID'])}</title>
<style>
body{{font-family:Segoe UI,Arial,sans-serif;margin:24px;color:#222}}
h1{{font-size:20px}} h2{{font-size:16px;margin-top:24px}}
table{{border-collapse:collapse;width:100%;font-size:13px}}
th,td{{border:1px solid #ccc;padding:6px;text-align:left;vertical-align:top}}
th{{background:#f3f3f3;width:28%}}
.badge{{display:inline-block;padding:4px 10px;border-radius:4px;background:#eee;margin-right:8px}}
.foot{{margin-top:24px;font-size:12px;color:#666}}
</style></head><body>
<h1>MCH CX Review - {e(row['Review ID'])}</h1>
<span class="badge">Total {e(total)}</span>
<span class="badge">{e(detail['ai_status'])}</span>
<p><b>Review flags:</b> {e(flags)}<br>CX % and Risk Level are calculated by the tracker formulas.</p>
<h2>Measure Scores</h2>
<table><tr><th>Measure</th><th>AI</th><th>Final</th><th>Rules applied</th><th>Rationale</th><th>Evidence</th></tr>{drows}</table>
<h2>Tracker Fields</h2>
<table>{info}</table>
<p class="foot">Automated draft from a machine transcript. Verify against the recording before use for coaching. Standard dispute process applies.</p>
</body></html>"""

    def row_html(self) -> Message:
        row, detail, _s, _c = self._build()
        return Message(text=self._html(row, detail))
