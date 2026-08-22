"""Generate the READ/WRITE tool table for docs/mcp_tools.md from the live enumeration."""
import json, os
here = os.path.dirname(__file__)
tools = json.load(open(os.path.join(here, "tools_raw.json")))["tools"]
m = json.load(open(os.path.join(here, "toolset_map.json")))

WRITE = {
    "place_stock_order","place_crypto_order","place_option_order","replace_order_by_id",
    "cancel_order_by_id","cancel_all_orders","close_position","close_all_positions",
    "exercise_options_position","do_not_exercise_options_position","update_account_config",
    "create_watchlist","update_watchlist_by_id","delete_watchlist_by_id",
    "add_asset_to_watchlist_by_id","remove_asset_from_watchlist_by_id","create_locate",
}
ALWAYS = set(m["_always_on"])

def toolset_of(name):
    if name in ALWAYS: return "(always-on)"
    for k, v in m.items():
        if k.endswith("_exclusive") and name in v:
            return k[:-len("_exclusive")]
    return "(none)"

ALLOWLIST = {
    "get_clock","get_account_info","get_all_positions","get_open_position","get_orders",
    "get_option_chain","get_option_snapshot","get_option_contracts","get_stock_bars",
    "get_stock_latest_trade","get_stock_snapshot",
}

groups = {}
for t in tools:
    groups.setdefault(toolset_of(t["name"]), []).append(t)

order = ["account","trading","assets","stock-data","options-data","crypto-data",
         "watchlists","corporate-actions","news","fixed-income-data","index-data",
         "(none)","(always-on)"]

lines, nread, nwrite = [], 0, 0
for g in order:
    if g not in groups: continue
    lines.append(f"\n#### `{g}`\n")
    lines.append("| Tool | R/W | Allowlisted | Description |")
    lines.append("|---|---|---|---|")
    for t in sorted(groups[g], key=lambda x: x["name"]):
        n = t["name"]
        rw = "**WRITE**" if n in WRITE else "READ"
        nwrite += n in WRITE; nread += n not in WRITE
        mark = "✅" if n in ALLOWLIST else ""
        desc = " ".join((t["description"] or "").split())
        desc = (desc[:95] + "…") if len(desc) > 95 else desc
        lines.append(f"| `{n}` | {rw} | {mark} | {desc} |")

print("\n".join(lines))
print(f"\n<!-- totals: {nread} READ / {nwrite} WRITE / {nread+nwrite} total -->")
