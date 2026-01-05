import json
from dataclasses import dataclass
from typing import Literal, Optional, Dict, Any

Action = Literal["VALIDATE_INPUTS", "PARSE_LC", "PARSE_DOCS", "RUN_CHECK", "REVIEW", "FINALIZE", "STOP"]
CheckType = Literal["amount_consistency", "date_consistency"]

@dataclass
class Decision:
    action: Action
    check_type: Optional[CheckType] = None
    reason: str = ""

def tool_validate_inputs(state: Dict[str, Any]) -> None:
    # TODO: fill with true validation logic
    state["validated"] = True

def tool_parse_lc(state: Dict[str, Any]) -> None:
    # TODO: fill with true lc parsing logic
    state["lc_parsed"] = True

def tool_parse_docs(state: Dict[str, Any]) -> None:
    # TODO: fill with true presentation document parsing logic
    state["docs_parsed"] = True

def tool_run_check(state: Dict[str, Any], check_type: CheckType) -> None:
    # TODO: fill with true LLM doc checker logic
    state["checks"][check_type] = "done"
    # simulation：date issue
    if check_type == "date_consistency":
        state["findings"].append({"type": "date_mismatch", "detail": "Simulated finding"})

def tool_review(state: Dict[str, Any]) -> None:
    # TODO: fill with true LLM doc check review logic
    state["reviewed"] = True

def tool_finalize(state: Dict[str, Any]) -> None:
    state["finalized"] = True

# ---------- Policy ----------
def decide_next(state: Dict[str, Any]) -> Decision:
    if state["budget"]["steps_used"] >= state["budget"]["max_steps"]:
        return Decision(action="STOP", reason="Budget exhausted")

    if not state["validated"]:
        return Decision(action="VALIDATE_INPUTS", reason="Inputs not validated")

    if not state["lc_parsed"]:
        return Decision(action="PARSE_LC", reason="LC not parsed")

    if not state["docs_parsed"]:
        return Decision(action="PARSE_DOCS", reason="Docs not parsed")

    if state["checks"]["amount_consistency"] == "not_run":
        return Decision(action="RUN_CHECK", check_type="amount_consistency", reason="Run amount consistency first")

    if state["checks"]["date_consistency"] == "not_run":
        return Decision(action="RUN_CHECK", check_type="date_consistency", reason="Run date consistency next")

    if (len(state["findings"]) > 0) and (not state["reviewed"]):
        return Decision(action="REVIEW", reason="Findings exist, need review")

    if not state["finalized"]:
        return Decision(action="FINALIZE", reason="All checks done, finalize output")

    return Decision(action="STOP", reason="Workflow finished")

# ---------- Runner ----------
def apply_decision(state: Dict[str, Any], d: Decision) -> None:
    print(f"\n[DECISION] action={d.action} check={d.check_type} reason={d.reason}")

    if d.action == "VALIDATE_INPUTS":
        tool_validate_inputs(state)
    elif d.action == "PARSE_LC":
        tool_parse_lc(state)
    elif d.action == "PARSE_DOCS":
        tool_parse_docs(state)
    elif d.action == "RUN_CHECK":
        assert d.check_type is not None
        tool_run_check(state, d.check_type)
    elif d.action == "REVIEW":
        tool_review(state)
    elif d.action == "FINALIZE":
        tool_finalize(state)
    elif d.action == "STOP":
        pass
    else:
        raise ValueError(f"Unknown action: {d.action}")

def main():
    with open("state.json", "r", encoding="utf-8") as f:
        state = json.load(f)

    while True:
        state["budget"]["steps_used"] += 1

        d = decide_next(state)
        apply_decision(state, d)

        #
        print("[STATE] validated:", state["validated"],
              "lc_parsed:", state["lc_parsed"],
              "docs_parsed:", state["docs_parsed"],
              "checks:", state["checks"],
              "findings:", len(state["findings"]),
              "reviewed:", state["reviewed"],
              "finalized:", state["finalized"])

        if d.action == "STOP":
            break

    with open("state_out.json", "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

    print("\nDone. Output written to state_out.json")

if __name__ == "__main__":
    main()