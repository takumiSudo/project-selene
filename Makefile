# ─────────────────────────────────────────────────────────────────────────────
#  Project Selene — Makefile
#
#  Usage:
#    make run              Full pipeline: build → start colony → map → report
#    make up               Build and start all Docker services
#    make down             Stop all services
#    make clean            Stop services and wipe .artifacts/
#    make map              Trigger mapping job and tail logs until complete
#    make report           Trigger reporting job and tail logs until complete
#    make logs             Stream rover logs
#    make show-report      Print report.md to terminal
#    make show-map         Pretty-print map.json
#    make status           Show current job status from rover API
#
#  LLM API key (optional — deterministic fallbacks used if absent):
#    echo "LLM_API_KEY=sk-ant-..." >> .env
#    make run
#
#  Or inline (overrides .env):
#    make run LLM_API_KEY=sk-ant-...
# ─────────────────────────────────────────────────────────────────────────────

# Load .env if present; silently skip if missing.
# docker compose also reads .env natively, so keys reach the containers automatically.
-include .env
export LLM_API_KEY
export HELIOS_COOLANT_DEGRADATION_HOURS

LLM_API_KEY   ?=
COMPOSE       := docker compose
ROVER_URL     := http://localhost:8080
POLL_SEC      := 3
TIMEOUT_SEC   := 300

.PHONY: all run up down clean map report wait-healthy logs show-report show-map status check-env

all: run

# ── Full pipeline ─────────────────────────────────────────────────────────────

run: check-env up wait-healthy map report show-report

# ── Docker lifecycle ──────────────────────────────────────────────────────────

up:
	@echo ""
	@echo "═══════════════════════════════════════════════════════"
	@echo "  SELENE COLONY — BUILDING AND STARTING ALL SERVICES"
	@echo "═══════════════════════════════════════════════════════"
	@$(COMPOSE) up --build -d
	@echo "✓ Services started (gateway + 12 pods + rover)."

down:
	@$(COMPOSE) down --remove-orphans

clean:
	@$(COMPOSE) down --remove-orphans -v
	@rm -rf .artifacts
	@echo "✓ Services stopped and .artifacts/ wiped."

# ── Health gate ───────────────────────────────────────────────────────────────
#    Blocks until rover:8080/health responds 200 or TIMEOUT_SEC is exceeded.

wait-healthy:
	@echo ""
	@printf "  Waiting for rover to be healthy "
	@ELAPSED=0; \
	until curl -sf $(ROVER_URL)/health > /dev/null 2>&1; do \
		printf "."; \
		sleep 2; \
		ELAPSED=$$((ELAPSED + 2)); \
		if [ $$ELAPSED -ge $(TIMEOUT_SEC) ]; then \
			echo ""; \
			echo "✗ Rover did not become healthy within $(TIMEOUT_SEC)s."; \
			echo "  Logs: make logs"; \
			exit 1; \
		fi; \
	done
	@echo " ready."

# ── Mapping job ───────────────────────────────────────────────────────────────
#    POST /map → tail rover logs in background → poll /get-map until done/error.

map: wait-healthy
	@echo ""
	@echo "═══════════════════════════════════════════════════════"
	@echo "  PHASE: MAPPING  (discovery → crawl → metrics → map.json)"
	@echo "═══════════════════════════════════════════════════════"
	@HTTP_CODE=$$(curl -s -o /dev/null -w "%{http_code}" -X POST $(ROVER_URL)/map); \
	if [ "$$HTTP_CODE" = "409" ]; then \
		echo "⚠  Mapping job already running — polling existing job."; \
	elif [ "$$HTTP_CODE" != "202" ]; then \
		echo "✗ Failed to start mapping job (HTTP $$HTTP_CODE)."; \
		exit 1; \
	fi; \
	$(COMPOSE) logs --tail=0 -f rover 2>&1 & LOGS_PID=$$!; \
	trap 'kill $$LOGS_PID 2>/dev/null || true' EXIT INT TERM; \
	ELAPSED=0; \
	while true; do \
		sleep $(POLL_SEC); \
		HTTP=$$(curl -s -o /tmp/.selene_map_body -w "%{http_code}" $(ROVER_URL)/get-map 2>/dev/null || echo "000"); \
		if [ "$$HTTP" = "200" ]; then \
			kill $$LOGS_PID 2>/dev/null || true; \
			echo ""; \
			echo "✓ Mapping complete → .artifacts/map.json"; \
			break; \
		fi; \
		if [ "$$HTTP" = "500" ]; then \
			kill $$LOGS_PID 2>/dev/null || true; \
			echo ""; \
			echo "✗ Mapping failed. Rover error:"; \
			python3 -c "import json,sys; d=json.load(open('/tmp/.selene_map_body')); print(d.get('error','(no message)'))" 2>/dev/null || cat /tmp/.selene_map_body; \
			echo ""; \
			echo "Full log → .artifacts/.map.log"; \
			exit 1; \
		fi; \
		ELAPSED=$$((ELAPSED + $(POLL_SEC))); \
		if [ $$ELAPSED -ge $(TIMEOUT_SEC) ]; then \
			kill $$LOGS_PID 2>/dev/null || true; \
			echo ""; \
			echo "✗ Mapping timed out after $(TIMEOUT_SEC)s."; \
			exit 1; \
		fi; \
	done

# ── Reporting job ─────────────────────────────────────────────────────────────
#    POST /report → tail rover logs in background → poll /get-report until done/error.

report: wait-healthy
	@echo ""
	@echo "═══════════════════════════════════════════════════════"
	@echo "  PHASE: REPORTING  (map.json → report.md)"
	@echo "═══════════════════════════════════════════════════════"
	@HTTP_CODE=$$(curl -s -o /dev/null -w "%{http_code}" -X POST $(ROVER_URL)/report); \
	if [ "$$HTTP_CODE" = "409" ]; then \
		echo "⚠  Reporting job already running — polling existing job."; \
	elif [ "$$HTTP_CODE" != "202" ]; then \
		echo "✗ Failed to start reporting job (HTTP $$HTTP_CODE)."; \
		exit 1; \
	fi; \
	$(COMPOSE) logs --tail=0 -f rover 2>&1 & LOGS_PID=$$!; \
	trap 'kill $$LOGS_PID 2>/dev/null || true' EXIT INT TERM; \
	ELAPSED=0; \
	while true; do \
		sleep $(POLL_SEC); \
		HTTP=$$(curl -s -o /tmp/.selene_report_body -w "%{http_code}" $(ROVER_URL)/get-report 2>/dev/null || echo "000"); \
		if [ "$$HTTP" = "200" ]; then \
			kill $$LOGS_PID 2>/dev/null || true; \
			echo ""; \
			echo "✓ Reporting complete → .artifacts/report.md"; \
			break; \
		fi; \
		if [ "$$HTTP" = "500" ]; then \
			kill $$LOGS_PID 2>/dev/null || true; \
			echo ""; \
			echo "✗ Reporting failed. Rover error:"; \
			python3 -c "import json,sys; d=json.load(open('/tmp/.selene_report_body')); print(d.get('error','(no message)'))" 2>/dev/null || cat /tmp/.selene_report_body; \
			echo ""; \
			echo "Full log → .artifacts/.report.log"; \
			exit 1; \
		fi; \
		ELAPSED=$$((ELAPSED + $(POLL_SEC))); \
		if [ $$ELAPSED -ge $(TIMEOUT_SEC) ]; then \
			kill $$LOGS_PID 2>/dev/null || true; \
			echo ""; \
			echo "✗ Reporting timed out after $(TIMEOUT_SEC)s."; \
			exit 1; \
		fi; \
	done

# ── Utilities ─────────────────────────────────────────────────────────────────

logs:
	$(COMPOSE) logs -f rover

show-report:
	@echo ""
	@echo "═══════════════════════════════════════════════════════"
	@echo "  REPORT  (.artifacts/report.md)"
	@echo "═══════════════════════════════════════════════════════"
	@echo ""
	@cat .artifacts/report.md 2>/dev/null \
		|| echo "(report.md not found — run 'make report' first)"

show-map:
	@cat .artifacts/map.json 2>/dev/null \
		| python3 -m json.tool \
		|| echo "(map.json not found — run 'make map' first)"

status:
	@echo ""
	@MAP_HTTP=$$(curl -s -o /dev/null -w "%{http_code}" $(ROVER_URL)/get-map 2>/dev/null || echo "000"); \
	RPT_HTTP=$$(curl -s -o /dev/null -w "%{http_code}" $(ROVER_URL)/get-report 2>/dev/null || echo "000"); \
	_decode() { case "$$1" in 200) echo "done";; 202) echo "running";; 404) echo "idle";; 500) echo "error";; 000) echo "(rover unavailable)";; *) echo "HTTP $$1";; esac; }; \
	echo "  Map job:    $$(_decode $$MAP_HTTP)"; \
	echo "  Report job: $$(_decode $$RPT_HTTP)"
	@echo ""

check-env:
	@if [ -z "$(LLM_API_KEY)" ]; then \
		echo ""; \
		echo "⚠  LLM_API_KEY is not set."; \
		echo "   Mapping Layer C (LLM enrichment) will be skipped."; \
		echo "   Reporter will use deterministic fallbacks for narrative sections."; \
		echo "   To enable LLM sections, add your key to .env:"; \
		echo "     echo 'LLM_API_KEY=sk-ant-...' >> .env"; \
		echo "   Or override inline: make run LLM_API_KEY=sk-ant-..."; \
		echo "   See .env.example for all available options."; \
		echo ""; \
	fi
