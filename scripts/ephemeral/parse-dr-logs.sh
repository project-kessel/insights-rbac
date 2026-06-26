#!/usr/bin/env bash
# Live-stream parser for RBAC worker/consumer/service logs.
# Prettifies all output with color-coded levels, DR event formatting,
# ECS JSON parsing, and inline JSON pretty-printing.
#
# Handles:
#   - Celery worker:  [2026-06-19 13:24:40,302: INFO/ForkPoolWorker-1] msg
#   - App logger:     [2026-06-19 13:24:40,302] INFO [env-...]: msg
#   - ECS JSON:       {"@timestamp":"...","log.level":"info",...}
#   - Django/gunicorn: plain text, warnings, tracebacks
#   - Truncated JSON (accumulates lines until complete)
#
# Usage:
#   oc logs <pod> -f | ./parse-dr-logs.sh
#   oc logs <pod> -f | ./parse-dr-logs.sh --json dr-logs.jsonl
#   ./parse-dr-logs.sh --pods rbac-kafka-consumer               # stream all matching pods
#   ./parse-dr-logs.sh --pods rbac-worker --tail 50             # last 50 lines + follow
#   ./parse-dr-logs.sh --pods rbac-worker --tail 0              # only new logs, no history
#   ./parse-dr-logs.sh --pods rbac-worker --json w.jsonl        # stream + save
#   ./parse-dr-logs.sh < logfile.txt
#   ./parse-dr-logs.sh --no-color logfile.txt
#
# --pods <pattern>  Find all pods matching grep pattern, stream all with --prefix.
# --tail <N>        Only show the last N log lines, then follow new output (--pods mode).
#                   Use --tail 0 to skip history entirely and only see new logs.
# --json <file>     Write every parsed record as JSONL for later analysis.
# --no-color        Disable ANSI colors.

set -euo pipefail

# ── Options ────────────────────────────────────────────────────────────────────
USE_COLOR=true
JSON_LOG=""
INPUT_FILE=""
POD_PATTERN=""
TAIL_LINES=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-color) USE_COLOR=false; shift ;;
        --json)
            [[ $# -lt 2 ]] && echo "Error: --json requires a filename" >&2 && exit 1
            JSON_LOG="$2"; shift 2 ;;
        --pods)
            [[ $# -lt 2 ]] && echo "Error: --pods requires a pattern" >&2 && exit 1
            POD_PATTERN="$2"; shift 2 ;;
        --tail)
            [[ $# -lt 2 ]] && echo "Error: --tail requires a number" >&2 && exit 1
            TAIL_LINES="$2"; shift 2 ;;
        --*)
            echo "Unknown option: $1" >&2; exit 1 ;;
        *)
            INPUT_FILE="$1"; shift ;;
    esac
done

if [[ "$USE_COLOR" == true ]] && [[ -t 1 || "${FORCE_COLOR:-}" == "1" ]]; then
    B=$'\033[1m'
    R=$'\033[31m'
    G=$'\033[32m'
    Y=$'\033[33m'
    C=$'\033[36m'
    M=$'\033[35m'
    D=$'\033[2m'
    N=$'\033[0m'
    BG_R=$'\033[41;97m'
    BG_Y=$'\033[43;30m'
else
    B="" R="" G="" Y="" C="" M="" D="" N="" BG_R="" BG_Y=""
fi

# ── --pods: find matching pods and stream all via oc logs --prefix ─────────────
if [[ -n "$POD_PATTERN" ]]; then
    pods=$(oc get pods -o name 2>/dev/null | grep -i "$POD_PATTERN" || true)
    if [[ -z "$pods" ]]; then
        echo "Error: no pods matching '$POD_PATTERN'" >&2
        exit 1
    fi

    pod_count=$(echo "$pods" | wc -l | tr -d ' ')
    printf "${D}── Streaming %s pod(s) matching '%s' ──${N}\n" "$pod_count" "$POD_PATTERN" >&2
    echo "$pods" | while IFS= read -r p; do
        printf "${D}   %s${N}\n" "$p" >&2
    done

    # Build passthrough args (everything except --pods)
    pass_args=()
    [[ "$USE_COLOR" == false ]] && pass_args+=(--no-color)
    [[ -n "$JSON_LOG" ]] && pass_args+=(--json "$JSON_LOG")

    # Spawn oc logs -f --prefix for each pod in background, all writing to stdout.
    # Pipe merged stdout through ourselves without --pods (avoids recursion).
    oc_pids=""
    cleanup_pids() {
        # shellcheck disable=SC2086
        kill $oc_pids 2>/dev/null || true
        wait 2>/dev/null || true
    }
    trap cleanup_pids EXIT INT TERM

    # Build oc logs flags
    oc_flags=(-f --prefix)
    [[ -n "$TAIL_LINES" ]] && oc_flags+=(--tail="$TAIL_LINES")

    {
        for pod in $pods; do
            oc logs "${oc_flags[@]}" "$pod" &
            oc_pids="$oc_pids $!"
        done
        wait 2>/dev/null || true
    } | "$0" "${pass_args[@]+"${pass_args[@]}"}"

    exit $?
fi

if [[ -n "$INPUT_FILE" ]]; then
    [[ ! -f "$INPUT_FILE" ]] && echo "Error: $INPUT_FILE not found" >&2 && exit 1
    exec < "$INPUT_FILE"
fi

# Initialize JSON log file
if [[ -n "$JSON_LOG" ]]; then
    : > "$JSON_LOG"
fi

# ── Check for python ──────────────────────────────────────────────────────────
HAS_PYTHON=false
command -v python3 &>/dev/null && HAS_PYTHON=true

# ── JSON log writer ───────────────────────────────────────────────────────────
# Appends one JSONL record. Args: category, level, message, timestamp, [key=value...]
write_json_record() {
    [[ -z "$JSON_LOG" ]] && return
    [[ "$HAS_PYTHON" != true ]] && return

    local category="$1" level="$2" message="$3" timestamp="$4"
    shift 4

    # Inject pod if set (cur_pod is global from main loop)
    local pod_extra=""
    [[ -n "$cur_pod" ]] && pod_extra="pod=$cur_pod"

    python3 -c "
import json, sys, re

record = {
    'timestamp': sys.argv[1],
    'level': sys.argv[2],
    'category': sys.argv[3],
    'message': sys.argv[4],
}

# Parse extra key=value pairs
i = 5
while i < len(sys.argv):
    arg = sys.argv[i]
    if '=' in arg:
        k, v = arg.split('=', 1)
        try:
            v = json.loads(v)
        except Exception:
            pass
        record[k] = v
    i += 1

print(json.dumps(record, ensure_ascii=False))
" "$timestamp" "$level" "$category" "$message" ${pod_extra:+"$pod_extra"} "$@" >> "$JSON_LOG"
}

# Write raw ECS JSON record directly (it's already structured)
write_ecs_record() {
    [[ -z "$JSON_LOG" ]] && return
    [[ "$HAS_PYTHON" != true ]] && return

    local raw="$1"
    local pod_name="${2:-}"

    python3 -c "
import json, sys

raw = sys.argv[1]
pod_name = sys.argv[2] if len(sys.argv) > 2 else ''
try:
    obj = json.loads(raw)
except Exception:
    obj = {'raw': raw}

# Normalize to flat structure
log_obj = obj.get('log', {})
record = {
    'timestamp': obj.get('@timestamp', ''),
    'level': obj.get('log.level', obj.get('level', 'info')),
    'category': 'ecs',
}
if pod_name:
    record['pod'] = pod_name

if isinstance(log_obj, dict):
    record['logger'] = log_obj.get('logger', '')
    record['message'] = log_obj.get('original', obj.get('message', ''))
    origin = log_obj.get('origin', {})
    if isinstance(origin, dict):
        f = origin.get('file', {})
        if f:
            record['source_file'] = f.get('name', '')
            record['source_line'] = f.get('line', '')
        record['function'] = origin.get('function', '')
else:
    record['message'] = obj.get('message', '')

# Classify by level
lvl = record['level'].lower()
if lvl == 'error':
    record['category'] = 'error'
elif lvl == 'warning':
    record['category'] = 'warning'

# Include env and extra fields
record['env_name'] = obj.get('env_name', '')
skip = {'@timestamp', 'log.level', 'level', 'log', 'message', 'ecs.version',
        'env_name', 'labels', 'process', 'service'}
extra = {k: v for k, v in obj.items() if k not in skip}
if extra:
    record['extra'] = extra

print(json.dumps(record, ensure_ascii=False))
" "$raw" "$pod_name" >> "$JSON_LOG"
}

# ── Helpers ────────────────────────────────────────────────────────────────────
level_color() {
    case "$1" in
        ERROR|error|CRITICAL) printf "%s" "$R" ;;
        WARNING|warning|warn) printf "%s" "$Y" ;;
        *)                    printf "%s" "" ;;
    esac
}

colorize_level() {
    case "$1" in
        ERROR|error|CRITICAL) printf "%s" "${BG_R} ${1} ${N}" ;;
        WARNING|warning|warn) printf "%s" "${BG_Y} WARN ${N}" ;;
        INFO|info)            printf "%s" "${C}INFO${N}" ;;
        DEBUG|debug)          printf "%s" "${D}DEBG${N}" ;;
        *)                    printf "%s" "${D}${1}${N}" ;;
    esac
}

format_tuple() {
    local tuple="$1"
    if [[ "$tuple" =~ ^([^:]+):([^#]+)#([^@]+)@(.+)$ ]]; then
        printf "${B}%s${N}${D}:${N}%s ${D}#${N}%s ${D}@${N} %s" \
            "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}" "${BASH_REMATCH[3]}" "${BASH_REMATCH[4]}"
    else
        printf "%s" "$tuple"
    fi
}

pretty_print_json() {
    local raw="$1"
    local indent="$2"
    local color="${3:-}"

    if [[ "$HAS_PYTHON" != true ]]; then
        printf "%s%s%s%s\n" "$color" "$indent" "$raw" "$N"
        return
    fi

    local formatted
    formatted=$(python3 -c "
import json, sys, re

indent_str = sys.argv[2]
raw = sys.argv[1]

def try_parse(s):
    try:
        return json.loads(s)
    except Exception:
        return None

obj = try_parse(raw)

if obj is None:
    s = raw.replace(\"'\", '\"')
    s = s.replace('True', 'true').replace('False', 'false').replace('None', 'null')
    s = re.sub(r',\s*\.\.\.\}', '}', s)
    s = re.sub(r',\s*\"\.\.\.\"', '', s)
    obj = try_parse(s)

if obj is not None:
    for line in json.dumps(obj, indent=2, ensure_ascii=False).splitlines():
        print(indent_str + line)
else:
    print(indent_str + raw)
" "$raw" "$indent" 2>&1)

    if [[ -n "$color" ]]; then
        while IFS= read -r jline; do
            printf "%s%s%s\n" "$color" "$jline" "$N"
        done <<< "$formatted"
    else
        printf "%s\n" "$formatted"
    fi
}

format_ecs_json() {
    local json_str="$1"
    local ptag="${2:-}"

    # Write to JSON log
    write_ecs_record "$json_str" "$cur_pod"

    if [[ "$HAS_PYTHON" != true ]]; then
        printf "          %s\n" "$json_str"
        return
    fi

    local ecs_level
    ecs_level=$(python3 -c "
import json, sys
try:
    obj = json.loads(sys.argv[1])
    print(obj.get('log.level', obj.get('level', 'info')))
except: print('info')
" "$json_str" 2>/dev/null)
    last_level=$(printf "%s" "$ecs_level" | tr '[:lower:]' '[:upper:]')

    local lc
    lc=$(level_color "$ecs_level")

    local formatted
    formatted=$(python3 -c "
import json, sys

raw = sys.argv[1]

try:
    obj = json.loads(raw)
except Exception:
    print(raw)
    sys.exit(0)

ts = obj.get('@timestamp', '')
short_ts = ''
if 'T' in ts:
    short_ts = ts.split('T')[1][:8]

level = obj.get('log.level', obj.get('level', ''))

log_obj = obj.get('log', {})
original = ''
if isinstance(log_obj, dict):
    original = log_obj.get('original', '')
message = obj.get('message', original or '')

logger = ''
if isinstance(log_obj, dict):
    logger = log_obj.get('logger', '')
if logger:
    parts = logger.rsplit('.', 1)
    logger = parts[-1] if len(parts) > 1 else logger

out = ''
if short_ts:
    out += short_ts + '  '

level_upper = (level.upper() if level else 'INFO').ljust(5)
out += level_upper + '  '

if logger:
    out += '[' + logger + '] '

if message:
    out += message

print(out)

skip = {'@timestamp', 'log.level', 'level', 'log', 'message', 'ecs.version',
        'env_name', 'labels', 'process', 'service'}
extra = {k: v for k, v in obj.items() if k not in skip}
if extra:
    for line in json.dumps(extra, indent=2, ensure_ascii=False).splitlines():
        print('          ' + line)
" "$json_str" 2>&1)

    local first=true
    if [[ -n "$lc" ]]; then
        while IFS= read -r eline; do
            if [[ "$first" == true && -n "$ptag" ]]; then
                printf "%s%s%s%s\n" "$ptag" "$lc" "$eline" "$N"
                first=false
            else
                printf "%s%s%s\n" "$lc" "$eline" "$N"
            fi
        done <<< "$formatted"
    else
        while IFS= read -r eline; do
            if [[ "$first" == true && -n "$ptag" ]]; then
                printf "%s%s\n" "$ptag" "$eline"
                first=false
            else
                printf "%s\n" "$eline"
            fi
        done <<< "$formatted"
    fi
}

# ── Pod colors (up to 6 distinct pods) ────────────────────────────────────────
POD_COLORS=("$C" "$M" "$G" "$Y" "$R" "$B")
pod_color_idx=0

# Returns a stable color for a pod name (assigns on first use)
pod_color_map=""  # "name1:0;name2:1;..."
get_pod_color() {
    local pod="$1"
    # Search in map
    local idx
    idx=$(echo "$pod_color_map" | tr ';' '\n' | grep "^${pod}:" | head -1 | cut -d: -f2)
    if [[ -z "$idx" ]]; then
        idx=$pod_color_idx
        pod_color_map="${pod_color_map}${pod}:${idx};"
        pod_color_idx=$(( (pod_color_idx + 1) % ${#POD_COLORS[@]} ))
    fi
    printf "%s" "${POD_COLORS[$idx]}"
}

format_pod_tag() {
    local pod="$1"
    [[ -z "$pod" ]] && return
    local pc
    pc=$(get_pod_color "$pod")
    # Shorten: pod/rbac-kafka-consumer-service-abc123 -> kafka-consumer
    local short="$pod"
    short="${short#pod/}"           # drop pod/ prefix
    short="${short#rbac-}"          # drop rbac- prefix
    short="${short%-service-*}"     # drop -service-HASH suffix
    short="${short%-[a-z0-9]*-[a-z0-9]*}" # drop replicaset hash
    # Fallback: if shortening removed everything, use last component
    if [[ -z "$short" || "$short" == "$pod" ]]; then
        short="${pod##*/}"
        short="${short%%-[a-z0-9][a-z0-9][a-z0-9][a-z0-9][a-z0-9]*}"
    fi
    printf "${pc}[%s]${N} " "$short"
}

# ── State ──────────────────────────────────────────────────────────────────────
last_msg=""
last_level=""
last_ts=""
json_buffer=""
traceback_buffer=""
cur_pod=""

dedup() {
    local msg="$1"
    [[ "$msg" == "$last_msg" ]] && return 1
    last_msg="$msg"
    return 0
}

flush_json_buffer() {
    if [[ -n "$json_buffer" ]]; then
        if [[ "$json_buffer" =~ \"@timestamp\" ]]; then
            format_ecs_json "$json_buffer" "$pod_tag"
        else
            local lc
            lc=$(level_color "$last_level")
            pretty_print_json "$json_buffer" "          " "$lc"
            write_json_record "json_data" "$last_level" "" "$last_ts" "data=$json_buffer"
        fi
        json_buffer=""
    fi
}

flush_traceback() {
    if [[ -n "$traceback_buffer" ]]; then
        write_json_record "traceback" "ERROR" "$traceback_buffer" "$last_ts"
        traceback_buffer=""
    fi
}

# ── Main loop ─────────────────────────────────────────────────────────────────
while IFS= read -r line; do

    # ── Strip oc logs --prefix pod tag ────────────────────────────────────
    # Format: "[pod/name container] actual log line" or "[pod/name] actual log line"
    cur_pod=""
    pod_tag=""
    case "$line" in
        "[pod/"*"] "* | "[deployment/"*"] "*)
            # Extract bracket contents and remainder
            local_bracket="${line%%] *}"
            local_bracket="${local_bracket#[}"   # strip leading [
            line="${line#*] }"                   # strip [prefix] from line
            # First word is the resource path (e.g. pod/name)
            cur_pod="${local_bracket%% *}"
            pod_tag=$(format_pod_tag "$cur_pod")
            ;;
    esac

    # ── JSON buffer: accumulate truncated JSON lines ──────────────────────
    if [[ -n "$json_buffer" ]]; then
        if [[ "$line" =~ ^\[ ]] || [[ "$line" =~ ^\{\"@timestamp\" ]]; then
            flush_json_buffer
        else
            json_buffer="${json_buffer}${line}"
            if python3 -c "import json,sys; json.loads(sys.argv[1])" "$json_buffer" 2>/dev/null; then
                flush_json_buffer
            fi
            continue
        fi
    fi

    # ── ECS JSON format ───────────────────────────────────────────────────
    if [[ "$line" =~ ^\{\"@timestamp\" ]] || [[ "$line" =~ ^\{\"log\.level\" ]] || [[ "$line" =~ ^\{\"ecs\.version\" ]]; then
        flush_traceback
        if python3 -c "import json,sys; json.loads(sys.argv[1])" "$line" 2>/dev/null; then
            format_ecs_json "$line" "$pod_tag"
        else
            json_buffer="$line"
        fi
        continue
    fi

    # ── Non-ECS JSON line (starts with { but no @timestamp) ─────────────
    if [[ "$line" =~ ^\{ ]]; then
        flush_traceback
        local_color=$(level_color "$last_level")
        if python3 -c "import json,sys; json.loads(sys.argv[1])" "$line" 2>/dev/null; then
            [[ -n "$pod_tag" ]] && printf "%s" "$pod_tag"
            pretty_print_json "$line" "          " "$local_color"
            write_json_record "json_data" "$last_level" "" "$last_ts" "data=$line"
        else
            json_buffer="$line"
        fi
        continue
    fi

    # ── Unstructured lines (no [ or { prefix) ─────────────────────────────
    if [[ ! "$line" =~ ^\[ ]] && [[ ! "$line" =~ ^\{ ]]; then

        # oc metadata (e.g. "Defaulted container...")
        if [[ "$line" == "Defaulted container"* ]]; then
            flush_traceback
            printf "%s${D}%s${N}\n" "$pod_tag" "$line"
            write_json_record "system" "INFO" "$line" ""
            last_level=""
            continue
        fi

        # Traceback lines
        is_traceback=false
        case "$line" in
            "Traceback"*|"  File "*) is_traceback=true ;;
        esac
        [[ "$line" =~ ^[A-Za-z]+(Error|Exception):\  ]] && is_traceback=true
        if [[ "$line" =~ ^\ {4}[^\ ] ]]; then
            case "$last_msg" in
                "  File "*|"Traceback"*) is_traceback=true ;;
            esac
        fi
        if [[ "$is_traceback" == true ]]; then
            printf "%s${R}%s${N}\n" "$pod_tag" "$line"
            last_msg="$line"
            last_level="ERROR"
            # Accumulate traceback for JSON log
            if [[ -n "$traceback_buffer" ]]; then
                traceback_buffer="${traceback_buffer}
${line}"
            else
                traceback_buffer="$line"
            fi
            continue
        fi

        # If we were accumulating a traceback and hit a non-traceback line, flush it
        flush_traceback

        # Python warnings (filepath:line: WarningType: message)
        if [[ "$line" =~ \.py:[0-9]+:.*Warning: ]]; then
            printf "%s${Y}          %s${N}\n" "$pod_tag" "$line"
            write_json_record "warning" "WARNING" "$line" "$last_ts"
            last_level="WARNING"
            continue
        fi
        # Indented source line following a warning
        if [[ "$last_level" == "WARNING" && "$line" =~ ^\ +[a-z] ]]; then
            printf "%s${Y}          %s${N}\n" "$pod_tag" "$line"
            continue
        fi

        # Continuation lines — inherit color from last structured log
        if [[ -n "$line" ]]; then
            local_color=$(level_color "$last_level")
            if [[ -n "$local_color" ]]; then
                printf "%s%s          %s%s\n" "$pod_tag" "$local_color" "$line" "$N"
            else
                printf "%s${D}          %s${N}\n" "$pod_tag" "$line"
            fi
            write_json_record "continuation" "${last_level:-INFO}" "$line" "$last_ts"
        fi
        continue
    fi

    # If we were accumulating a traceback, flush before structured line
    flush_traceback

    # ── Parse timestamp, level, message from Celery/app formats ──────────
    ts="" level="" msg=""

    # Format A: [2026-06-19 13:24:40,302] INFO [env-...]: message
    if [[ "$line" =~ ^\[([0-9]{4}-[0-9]{2}-[0-9]{2}\ [0-9]{2}:[0-9]{2}:[0-9]{2}),[0-9]+\]\ ([A-Z]+)\ \[([^]]+)\]:\ (.*) ]]; then
        ts="${BASH_REMATCH[1]}" level="${BASH_REMATCH[2]}" msg="${BASH_REMATCH[4]}"

    # Format B: [2026-06-19 13:24:40,302: INFO/ForkPoolWorker-1] message
    elif [[ "$line" =~ ^\[([0-9]{4}-[0-9]{2}-[0-9]{2}\ [0-9]{2}:[0-9]{2}:[0-9]{2}),[0-9]+:\ ([A-Z]+)/([^]]+)\]\ (.*) ]]; then
        ts="${BASH_REMATCH[1]}" level="${BASH_REMATCH[2]}" msg="${BASH_REMATCH[4]}"

    # Format C: partial (e.g. "PoolWorker-1] message")
    elif [[ "$line" =~ ^[A-Za-z]+-[0-9]+\]\ (.*) ]]; then
        msg="${BASH_REMATCH[1]}" level="INFO"

    # Fallback
    else
        msg="$line"
    fi

    dedup "$msg" || continue

    last_level="$level"
    last_ts="$ts"
    short_ts=""
    [[ -n "$ts" ]] && short_ts="${ts:11:8}"
    local_color=$(level_color "$level")

    # ── DR: would remove ──────────────────────────────────────────────────
    if [[ "$msg" == *"DRY RUN: would remove"* ]]; then
        tuple="" reason="" offset=""
        [[ "$msg" =~ would\ remove\ ([^ ]+)\ \(reason: ]] && tuple="${BASH_REMATCH[1]}"
        [[ "$msg" =~ reason:\ ([^,]+) ]]                  && reason="${BASH_REMATCH[1]}"
        [[ "$msg" =~ offset=([0-9]+) ]]                    && offset="${BASH_REMATCH[1]}"

        printf "%s${D}%s${N}  ${R}${B}REMOVE${N}  " "$pod_tag" "$short_ts"
        format_tuple "$tuple"
        printf "  ${D}offset=%s${N}\n" "$offset"
        printf "                  ${D}%s${N}\n" "$reason"

        write_json_record "dr_remove" "INFO" "$reason" "$ts" "tuple=$tuple" "offset=$offset"
        continue
    fi

    # ── DR: would add ─────────────────────────────────────────────────────
    if [[ "$msg" == *"DRY RUN: would add"* ]]; then
        tuple="" reason="" offset=""
        [[ "$msg" =~ would\ add\ ([^ ]+)\ \(reason: ]]   && tuple="${BASH_REMATCH[1]}"
        [[ "$msg" =~ reason:\ ([^,]+) ]]                  && reason="${BASH_REMATCH[1]}"
        [[ "$msg" =~ offset=([0-9]+) ]]                    && offset="${BASH_REMATCH[1]}"

        printf "%s${D}%s${N}  ${G}${B}ADD   ${N}  " "$pod_tag" "$short_ts"
        format_tuple "$tuple"
        printf "  ${D}offset=%s${N}\n" "$offset"
        printf "                  ${D}%s${N}\n" "$reason"

        write_json_record "dr_add" "INFO" "$reason" "$ts" "tuple=$tuple" "offset=$offset"
        continue
    fi

    # ── DR: remove (live run) ─────────────────────────────────────────────
    if [[ "$msg" == *"Removing tuple:"* ]]; then
        tuple=""
        [[ "$msg" =~ Removing\ tuple:\ ([^ ]+) ]] && tuple="${BASH_REMATCH[1]}"
        printf "%s${D}%s${N}  ${R}${B}REMOVE${N}  " "$pod_tag" "$short_ts"
        format_tuple "$tuple"
        printf "\n"

        write_json_record "dr_remove_live" "INFO" "Removing tuple" "$ts" "tuple=$tuple"
        continue
    fi

    # ── DR: add (live run) ────────────────────────────────────────────────
    if [[ "$msg" == *"Adding tuple:"* ]]; then
        tuple=""
        [[ "$msg" =~ Adding\ tuple:\ ([^ ]+) ]] && tuple="${BASH_REMATCH[1]}"
        printf "%s${D}%s${N}  ${G}${B}ADD   ${N}  " "$pod_tag" "$short_ts"
        format_tuple "$tuple"
        printf "\n"

        write_json_record "dr_add_live" "INFO" "Adding tuple" "$ts" "tuple=$tuple"
        continue
    fi

    # ── DR: summary line ──────────────────────────────────────────────────
    if [[ "$msg" == *"DR reconciliation"*"events="* ]]; then
        events="" tuples="" w_add="" w_rem="" skip="" dur="" mode=""
        [[ "$msg" =~ events=([0-9]+) ]]        && events="${BASH_REMATCH[1]}"
        [[ "$msg" =~ tuples=([0-9]+) ]]        && tuples="${BASH_REMATCH[1]}"
        [[ "$msg" =~ would_add=([0-9]+) ]]     && w_add="${BASH_REMATCH[1]}"
        [[ "$msg" =~ would_remove=([0-9]+) ]]   && w_rem="${BASH_REMATCH[1]}"
        [[ "$msg" =~ adds=([0-9]+) ]]           && w_add="${BASH_REMATCH[1]}"
        [[ "$msg" =~ removes=([0-9]+) ]]        && w_rem="${BASH_REMATCH[1]}"
        [[ "$msg" =~ skipped=([0-9]+) ]]        && skip="${BASH_REMATCH[1]}"
        [[ "$msg" =~ \(([0-9.]+)s\) ]]          && dur="${BASH_REMATCH[1]}"
        [[ "$msg" == *"DRY RUN"* ]] && mode="DRY RUN" || mode="LIVE"

        echo ""
        printf "%s${D}%s${N}  ${B}── DR %s Summary ──────────────────────────────────${N}\n" "$pod_tag" "$short_ts" "$mode"
        printf "          Events: ${C}%s${N}  Tuples: ${C}%s${N}  Add: ${G}%s${N}  Remove: ${R}%s${N}  Skip: ${D}%s${N}  ${D}(%ss)${N}\n" \
            "$events" "$tuples" "$w_add" "$w_rem" "$skip" "$dur"
        echo "        ${B}────────────────────────────────────────────────────${N}"

        write_json_record "dr_summary" "INFO" "DR reconciliation $mode" "$ts" \
            "events=$events" "tuples=$tuples" "adds=${w_add}" "removes=${w_rem}" \
            "skipped=${skip}" "duration_s=${dur}" "mode=$mode"
        continue
    fi

    # ── Task succeeded (with possible JSON/dict result) ───────────────────
    if [[ "$msg" == *"succeeded in"* ]]; then
        task_name="" dur=""
        [[ "$msg" =~ Task\ ([^[]+)\[ ]]            && task_name="${BASH_REMATCH[1]}"
        [[ "$msg" =~ succeeded\ in\ ([0-9.]+)s ]]  && dur="${BASH_REMATCH[1]}"
        short_task="${task_name##*.}"

        printf "%s${D}%s${N}  ${G}${B}DONE${N}  ${B}%s${N} ${D}finished in %ss${N}\n" \
            "$pod_tag" "$short_ts" "$short_task" "$dur"

        result_json=""
        if [[ "$msg" =~ succeeded\ in\ [0-9.]+s:\ (.*) ]]; then
            result_json="${BASH_REMATCH[1]}"
            pretty_print_json "$result_json" "          " ""
        fi

        write_json_record "task_done" "INFO" "$task_name succeeded" "$ts" \
            "task=$task_name" "duration_s=$dur" "result=$result_json"
        last_level="INFO"
        continue
    fi

    # ── Task received ─────────────────────────────────────────────────────
    if [[ "$msg" == *"Received task:"* ]]; then
        task_name=""
        [[ "$msg" =~ Received\ task:\ ([^[]+)\[ ]] && task_name="${BASH_REMATCH[1]}"
        short_task="${task_name##*.}"
        printf "%s${D}%s${N}  ${Y}${B}TASK${N}  ${B}%s${N} ${D}received${N}\n" \
            "$pod_tag" "$short_ts" "$short_task"

        write_json_record "task_received" "INFO" "$task_name received" "$ts" "task=$task_name"
        continue
    fi

    # ── Task failed ───────────────────────────────────────────────────────
    if [[ "$msg" == *"raised unexpected"* ]]; then
        printf "%s${D}%s${N}  ${R}${B}FAIL${N}  ${R}%s${N}\n" "$pod_tag" "$short_ts" "$msg"
        write_json_record "task_failed" "ERROR" "$msg" "$ts"
        last_level="ERROR"
        continue
    fi

    # ── Celery system messages ────────────────────────────────────────────
    if [[ "$msg" == *"celery@"* || "$msg" == *"Connected to"* || "$msg" == *"mingle:"* || "$msg" == *"pidbox:"* || "$msg" == *"ready."* ]]; then
        printf "%s${D}%s${N}  ${M}SYS ${N}  ${D}%s${N}\n" "$pod_tag" "$short_ts" "$msg"
        write_json_record "system" "INFO" "$msg" "$ts"
        continue
    fi

    # ── Generic line — detect embedded JSON/dicts ─────────────────────────
    json_part=""
    text_part="$msg"

    if [[ "$msg" =~ ^(.*[^{])(\{.+\})(.*)$ ]]; then
        text_part="${BASH_REMATCH[1]}"
        json_part="${BASH_REMATCH[2]}${BASH_REMATCH[3]}"
    elif [[ "$msg" =~ ^(\{.+\})$ ]]; then
        text_part=""
        json_part="${BASH_REMATCH[1]}"
    fi

    if [[ -n "$json_part" && ${#json_part} -gt 40 ]]; then
        if [[ -n "$short_ts" ]]; then
            printf "%s${D}%s${N}  $(colorize_level "$level")  %s%s%s\n" "$pod_tag" "$short_ts" "$local_color" "$text_part" "$N"
        else
            printf "%s          %s%s%s\n" "$pod_tag" "$local_color" "$text_part" "$N"
        fi
        pretty_print_json "$json_part" "          " "$local_color"
        write_json_record "log" "$level" "$text_part" "$ts" "data=$json_part"
    else
        if [[ -n "$short_ts" ]]; then
            printf "%s${D}%s${N}  $(colorize_level "$level")  %s%s%s\n" "$pod_tag" "$short_ts" "$local_color" "$msg" "$N"
        elif [[ -n "$msg" ]]; then
            printf "%s          %s%s%s\n" "$pod_tag" "$local_color" "$msg" "$N"
        fi
        write_json_record "log" "$level" "$msg" "$ts"
    fi

done

flush_traceback
flush_json_buffer

if [[ -n "$JSON_LOG" ]]; then
    count=$(wc -l < "$JSON_LOG" | tr -d ' ')
    printf "${D}── %s records written to %s ──${N}\n" "$count" "$JSON_LOG" >&2
fi
