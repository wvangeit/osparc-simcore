#
# Targets for DEVELOPMENT of tests/public-api
#
include ../../scripts/common.Makefile
include ../../scripts/common-package.Makefile



.PHONY: install-dev install-prod install-ci
install-dev install-prod install-ci: _check_venv_active ## install app in development/production or CI mode
	# installing in $(subst install-,,$@) mode
	@uv pip sync requirements/$(subst install-,,$@).txt


# Configuration files and default values
LOCUST_CONFIG_FILE := .locust.conf
AUTH_CREDS_FILE := .auth-credentials.env

# Default Database settings
PG_HOST := 127.0.0.1
PG_PORT := 5432
PG_USER := postgres
PG_PASSWORD := password

# Default Grafana settings
GRAFANA_URL := http://127.0.0.1:3000/

# Default Locust test settings
DEFAULT_PROCESSES := 4
DEFAULT_USERS := 10
DEFAULT_SPAWN_RATE := 1
DEFAULT_RUN_TIME := 1m
DEFAULT_HOST := http://127.0.0.1:9081



define create_locust_config
	@if [ ! -f $(LOCUST_CONFIG_FILE) ]; then \
		printf "$(YELLOW)First time setup: Creating Locust configuration file$(NC)\n"; \
		printf "Available locust files in locustfiles/:\n"; \
		find locustfiles -maxdepth 1 -type f -name '*.py' ! -name '__init__.py' -printf '  %p\n'; \
		find locustfiles -mindepth 2 -type f -name 'workflow.py' -printf '  %p\n'; \
		read -p "Locust file to use [./locustfiles/deployment_max_rps_single_endpoint.py]: " locustfile; \
		locustfile=$${locustfile:-./locustfiles/deployment_max_rps_single_endpoint.py}; \
		read -p "Number of processes [$(DEFAULT_PROCESSES)]: " processes; \
		processes=$${processes:-$(DEFAULT_PROCESSES)}; \
		read -p "Number of users [$(DEFAULT_USERS)]: " users; \
		users=$${users:-$(DEFAULT_USERS)}; \
		read -p "Spawn rate [$(DEFAULT_SPAWN_RATE)]: " spawn_rate; \
		spawn_rate=$${spawn_rate:-$(DEFAULT_SPAWN_RATE)}; \
		read -p "Run time [$(DEFAULT_RUN_TIME)]: " run_time; \
		run_time=$${run_time:-$(DEFAULT_RUN_TIME)}; \
		read -p "Host to load test [$(DEFAULT_HOST)]: " host; \
		host=$${host:-$(DEFAULT_HOST)}; \
		echo; \
		echo "# Locust configuration file - Autogenerated" > $(LOCUST_CONFIG_FILE); \
		echo "[locust]" >> $(LOCUST_CONFIG_FILE); \
		echo "locustfile = $$locustfile" >> $(LOCUST_CONFIG_FILE); \
		echo "host = $$host" >> $(LOCUST_CONFIG_FILE); \
		echo "users = $$users" >> $(LOCUST_CONFIG_FILE); \
		echo "spawn-rate = $$spawn_rate" >> $(LOCUST_CONFIG_FILE); \
		echo "run-time = $$run_time" >> $(LOCUST_CONFIG_FILE); \
		echo "processes = $$processes" >> $(LOCUST_CONFIG_FILE); \
		echo "loglevel = INFO" >> $(LOCUST_CONFIG_FILE); \
		echo "" >> $(LOCUST_CONFIG_FILE); \
		printf "$(GREEN)Locust configuration file created. It won't be asked again.$(NC)\n"; \
	else \
		printf "$(GREEN)Using existing Locust configuration file $(LOCUST_CONFIG_FILE)$(NC)\n"; \
	fi
endef

# Function to prompt for credentials if they don't exist
define prompt_for_credentials
	@if [ ! -f $(AUTH_CREDS_FILE) ]; then \
		printf "$(YELLOW)First time setup: Please enter the deployment credentials$(NC)\n"; \
		read -p "Username: " username; \
		read -sp "Password: " password; \
		echo; \
		echo "SC_USER_NAME=$$username" > $(AUTH_CREDS_FILE); \
		echo "SC_PASSWORD=$$password" >> $(AUTH_CREDS_FILE); \
		read -p "osparc Username (required if login in osparc is necessary, press enter to skip): " osparc_username; \
		if [ ! -z "$$osparc_username" ]; then \
			read -sp "osparc Password: " osparc_password; \
			echo; \
			echo "OSPARC_USER_NAME=$$osparc_username" >> $(AUTH_CREDS_FILE); \
			echo "OSPARC_PASSWORD=$$osparc_password" >> $(AUTH_CREDS_FILE); \
		fi; \
		printf "$(GREEN)Credentials saved. They won't be asked again.$(NC)\n"; \
	else \
		printf "$(GREEN)Using cached credentials from $(AUTH_CREDS_FILE)$(NC)\n"; \
	fi
endef


test-deployment: _check_venv_active ## runs deployment test on deploy
	@$(call prompt_for_credentials)
	@$(call create_locust_config)
	@printf "$(YELLOW)Starting Locust...$(NC)\n"
	@xdg-open http://localhost:8089/ &
	@export $$(cat $(AUTH_CREDS_FILE) | xargs) && \
	locust --config $(LOCUST_CONFIG_FILE)

test-deployment-ci: _check_venv_active $(AUTH_CREDS_FILE) $(LOCUST_CONFIG_FILE) ## runs deployment test on CI, expects all config and credentials files to be present, does not prompt
	@printf "$(YELLOW)Starting Locust headless...$(NC)\n"
	@export $$(cat $(AUTH_CREDS_FILE) | xargs) && \
	locust --config $(LOCUST_CONFIG_FILE) \
		--headless \
		--html test_report_{u}users_{r}userspawnrate_{t}s.html

test-deployment-with-grafana: _check_venv_active grafana-dashboards-up ## runs deployment test with Grafana integration
	@$(call prompt_for_credentials)
	@$(call create_locust_config)
	@printf "$(YELLOW)Starting Locust with Grafana integration...$(NC)\n"
	@xdg-open $(GRAFANA_URL)
	@export $$(cat $(AUTH_CREDS_FILE) | xargs) && \
	locust --config $(LOCUST_CONFIG_FILE) \
		--grafana-url $(GRAFANA_URL) \
		--pghost $(PG_HOST) \
		--pgport $(PG_PORT) \
		--pguser $(PG_USER) \
		--pgpassword=$(PG_PASSWORD) \
		--headless \
		--timescale




clear-credentials: ## Clear the cached authentication credentials
	@if [ -f $(AUTH_CREDS_FILE) ]; then \
		rm $(AUTH_CREDS_FILE); \
		printf "$(GREEN)Credentials cleared.$(NC)\n"; \
	else \
		printf "$(YELLOW)No credentials file found.$(NC)\n"; \
	fi


clear: down clear-credentials clear-locust-config ## Clear all cached data including credentials and Locust configuration files

clear-locust-config: ## Clear all Locust configuration files
	@for config_file in $(LOCUST_CONFIG_FILE) $(LOCUST_CONFIG_FILE)_light $(LOCUST_CONFIG_FILE)_heavy; do \
		if [ -f $$config_file ]; then \
			rm $$config_file; \
			printf "$(GREEN)$$config_file cleared.$(NC)\n"; \
		fi; \
	done
	@if [ ! -f $(LOCUST_CONFIG_FILE) ] && [ ! -f $(LOCUST_CONFIG_FILE)_light ] && [ ! -f $(LOCUST_CONFIG_FILE)_heavy ]; then \
		printf "$(YELLOW)No Locust configuration files found.$(NC)\n"; \
	fi

grafana-dashboards-up: ## start grafana dashboards for locust
	@locust-compose up -d
down grafana-dashboards-down: ## stop grafana dashboards for locust
	@locust-compose down
