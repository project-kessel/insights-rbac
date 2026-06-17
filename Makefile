PYTHON	= $(shell which python)

TOPDIR  = $(shell pwd)
PYDIR	= rbac

OC_SOURCE	= registry.access.redhat.com/openshift3/ose
OC_VERSION	= v3.9
OC_DATA_DIR	= ${HOME}/.oc/openshift.local.data

PGSQL_VERSION   = 16

PORT=8000
APP_HOME=$(shell pwd)/$(PYDIR)
APP_MODULE=rbac.wsgi
APP_CONFIG=$(TOPDIR)/$(PYDIR)/gunicorn.py

OS := $(shell uname)
ifeq ($(OS),Darwin)
	PREFIX	=
else
	PREFIX	= sudo
endif

define HELP_TEXT
Please use `make <target>` where <target> is one of:

--- General Commands ---
  clean                    clean the project directory of any scratch files, bytecode, logs, etc.
  help                     show this message
  html                     create html documentation for the project
  lint                     run linting against the project
  format                   format linting errors found by lint task
  typecheck                run type check

--- Commands using local services ---
  create-test-db-file      create a Postgres DB dump file for RBAC
  collect-static           collect static files to host
  kafka-consumer           run the RBAC Kafka consumer with validation
  kafka-consumer-debug     run the RBAC Kafka consumer with debug logging
  make-migrations          make migrations for the database
  reinitdb                 drop and recreate the database
  run-migrations           run migrations against database
  serve                    run the Django server locally
  serve-with-oc            run Django server locally against an Openshift DB
  start-db                 start the psql db in detached state
  stop-compose             stop all containers
  unittest                 run unittests
  unittest-fast            run unittests without coverage (faster)
  unittest-profile         run unittests and show slowest tests
  user                     create a Django super user

--- Commands using Docker Compose ---
  docker-up                 run django and database
  docker-down               shut down service containers
  docker-shell              run django and db containers with shell access to server (for pdb)
  docker-logs               connect to console logs for all services
  docker-grype				Run security checks on the project image(s)

--- Commands using an OpenShift Cluster ---
  oc-clean                 stop openshift cluster & remove local config data
  oc-create-all            run all application services in openshift cluster
  oc-create-db             create a Postgres DB in an initialized openshift cluster
  oc-create-rbac           create the RBAC app in an initialized openshift cluster
  oc-create-tags           create image stream tags
  oc-create-test-db-file   create a Postgres DB dump file for RBAC
  oc-delete-all            delete Openshift objects without a cluster restart
  oc-down                  stop app & openshift cluster
  oc-forward-ports         port forward the DB to localhost
  oc-login-dev             login to an openshift cluster as 'developer'
  oc-reinit                remove existing app and restart app in initialized openshift cluster
  oc-run-migrations        run Django migrations in the Openshift DB
  oc-stop-forwarding-ports stop port forwarding the DB to localhost
  oc-up                    initialize an openshift cluster
  oc-up-all                run app in openshift cluster
  oc-up-db                 run Postgres in an openshift cluster
endef
export HELP_TEXT

help:
	@echo "$$HELP_TEXT"

clean:
	git clean -fdx -e .idea/ -e *env/

html:
	@pipenv run sphinx-build -b html docs/source docs/_build/html

lint:
	tox -elint

format:
	black -t py312 -l 119 rbac tests

typecheck:
	mypy --install-types --non-interactive rbac

reinitdb:
	make start-db
	make reset-db
	make run-migrations

reset-db:
	docker-compose exec -u postgres db dropdb postgres
	docker-compose exec -u postgres db createdb -Eutf8 -Ttemplate0 -Opostgres postgres

make-migrations:
	DJANGO_READ_DOT_ENV_FILE=True $(PYTHON) $(PYDIR)/manage.py makemigrations api management

run-migrations:
	DJANGO_READ_DOT_ENV_FILE=True $(PYTHON) $(PYDIR)/manage.py migrate

shell:
	DJANGO_READ_DOT_ENV_FILE=True $(PYTHON) $(PYDIR)/manage.py shell

seeds:
	DJANGO_READ_DOT_ENV_FILE=True $(PYTHON) $(PYDIR)/manage.py seeds

seeds-force-update:
	DJANGO_READ_DOT_ENV_FILE=True $(PYTHON) $(PYDIR)/manage.py seeds --force-update-relations

show-migrations:
	DJANGO_READ_DOT_ENV_FILE=True $(PYTHON) $(PYDIR)/manage.py showmigrations api management

urls:
	DJANGO_READ_DOT_ENV_FILE=True $(PYTHON) $(PYDIR)/manage.py show_urls

kafka-consumer:
	KAFKA_ENABLED=true RBAC_KAFKA_CONSUMER_TOPIC=outbox.event.rbac-consumer-replication-event RBAC_KAFKA_CONSUMER_GROUP_ID=rbac-consumer-group pipenv run python $(PYDIR)/manage.py launch-rbac-kafka-consumer

kafka-consumer-debug:
	KAFKA_ENABLED=true RBAC_KAFKA_CONSUMER_TOPIC=outbox.event.rbac-consumer-replication-event RBAC_KAFKA_CONSUMER_GROUP_ID=rbac-consumer-group DJANGO_LOG_LEVEL=DEBUG pipenv run python $(PYDIR)/manage.py launch-rbac-kafka-consumer


create-test-db-file: run-migrations
	sleep 1
	DJANGO_READ_DOT_ENV_FILE=True $(PYTHON) $(PYDIR)/manage.py runserver > /dev/null 2>&1 &
	sleep 5
	$(PYTHON) $(TOPDIR)/scripts/create_test_customer.py --bypass-api
	pg_dump -d $(DATABASE_NAME) -h $(POSTGRES_SQL_SERVICE_HOST) -p $(POSTGRES_SQL_SERVICE_PORT) -U $(DATABASE_USER) > test.sql
	kill -HUP $$(ps -eo pid,command | grep "manage.py runserver" | grep -v grep | awk '{print $$1}')


collect-static:
	$(PYTHON) $(PYDIR)/manage.py collectstatic --no-input

serve:
	DJANGO_READ_DOT_ENV_FILE=True $(PYTHON) $(PYDIR)/manage.py runserver $(PORT)

gunicorn-serve:
	DJANGO_READ_DOT_ENV_FILE=True gunicorn "$(APP_MODULE)" --chdir=$(APP_HOME) --bind=0.0.0.0:8080 --access-logfile=- --config "$(APP_CONFIG)" --preload

serve-with-oc: oc-forward-ports
	sleep 3
	DJANGO_READ_DOT_ENV_FILE=True $(PYTHON) $(PYDIR)/manage.py runserver
	make oc-stop-forwarding-ports

start-db:
	docker-compose up -d db

stop-compose:
	docker-compose down

unittest:
	$(PYTHON) $(PYDIR)/manage.py test $(PYDIR) -v 2

unittest-fast:
	tox -e py312-fast

unittest-profile:
	tox -e py312-profile

user:
	$(PYTHON) $(PYDIR)/manage.py createsuperuser

oc-clean: oc-down
	$(PREFIX) rm -rf $(OC_DATA_DIR)

oc-create-tags:
	oc get istag postgresql:$(PGSQL_VERSION) || oc create istag postgresql:$(PGSQL_VERSION) --from-image=centos/postgresql-96-centos7

oc-create-db:
	oc process openshift//postgresql-persistent \
		-p NAMESPACE=myproject \
		-p POSTGRESQL_USER=rbacadmin \
		-p POSTGRESQL_PASSWORD=admin123 \
		-p POSTGRESQL_DATABASE=rbac \
		-p POSTGRESQL_VERSION=$(PGSQL_VERSION) \
		-p DATABASE_SERVICE_NAME=rbac-pgsql \
	| oc create -f -

oc-create-all: oc-create-tags oc-create-rbac oc-create-redis oc-create-worker oc-create-scheduler

oc-create-rbac:
	openshift/init-app.sh -n myproject -b `git rev-parse --abbrev-ref HEAD`

oc-create-worker:
	oc get bc/rbac-worker dc/rbac-worker || \
	oc process -f $(TOPDIR)/openshift/worker.yaml \
		--param-file=$(TOPDIR)/openshift/worker.env \
		-p SOURCE_REPOSITORY_REF=$(shell git rev-parse --abbrev-ref HEAD) \
	| oc create -f -

oc-create-scheduler:
	oc get bc/rbac-scheduler dc/rbac-scheduler || \
	oc process -f $(TOPDIR)/openshift/scheduler.yaml \
		--param-file=$(TOPDIR)/openshift/scheduler.env \
		-p SOURCE_REPOSITORY_REF=$(shell git rev-parse --abbrev-ref HEAD) \
	| oc create -f -

oc-create-redis:
	oc get bc/rbac-redis dc/rbac-redis || \
	oc process -f $(TOPDIR)/openshift/redis.yaml \
		--param-file=$(TOPDIR)/openshift/redis.env \
		-p SOURCE_REPOSITORY_REF=$(shell git rev-parse --abbrev-ref HEAD) \
	| oc create -f -

oc-create-test-db-file: oc-run-migrations
	sleep 1
	make oc-forward-ports
	sleep 1
	DJANGO_READ_DOT_ENV_FILE=True $(PYTHON) $(PYDIR)/manage.py runserver > /dev/null 2>&1 &
	sleep 5
	$(PYTHON) $(TOPDIR)/scripts/create_test_customer.py --bypass-api
	pg_dump -d $(DATABASE_NAME) -h $(POSTGRES_SQL_SERVICE_HOST) -p $(POSTGRES_SQL_SERVICE_PORT) -U $(DATABASE_USER) > test.sql
	kill -HUP $$(ps -eo pid,command | grep "manage.py runserver" | grep -v grep | awk '{print $$1}')
	make oc-stop-forwarding-ports

oc-delete-scheduler:
	oc delete deploymentconfigs/rbac-scheduler  \
		buildconfigs/rbac-scheduler \
		imagestreams/rbac-scheduler \

oc-delete-worker:
	oc delete deploymentconfigs/rbac-worker  \
		buildconfigs/rbac-worker \
		imagestreams/rbac-worker \

oc-delete-redis:
	oc delete deploymentconfigs/rbac-redis  \
		buildconfigs/rbac-redis \
		imagestreams/rbac-redis \

oc-delete-all:
	oc delete is --all && \
	oc delete dc --all && \
	oc delete bc --all && \
	oc delete svc --all && \
	oc delete pvc --all && \
	oc delete routes --all && \
	oc delete statefulsets --all && \
	oc delete configmap/rbac-env \
		secret/rbac-secret \
		secret/rbac-pgsql \

oc-down:
	oc cluster down

oc-forward-ports:
	-make oc-stop-forwarding-ports 2>/dev/null
	oc port-forward $$(oc get pods -o jsonpath='{.items[*].metadata.name}' -l name=rbac-pgsql) 15432:5432 >/dev/null 2>&1 &

oc-login-dev:
	oc login -u developer --insecure-skip-tls-verify=true localhost:8443

oc-make-migrations: oc-forward-ports
	sleep 3
	DJANGO_READ_DOT_ENV_FILE=True $(PYTHON) $(PYDIR)/manage.py makemigrations api management
	make oc-stop-forwarding-ports

oc-reinit: oc-delete-all oc-create-rbac

oc-run-migrations: oc-forward-ports
	sleep 3
	DJANGO_READ_DOT_ENV_FILE=True $(PYTHON) $(PYDIR)/manage.py migrate
	make oc-stop-forwarding-ports

oc-stop-forwarding-ports:
	kill -HUP $$(ps -eo pid,command | grep "oc port-forward" | grep -v grep | awk '{print $$1}')

oc-up:
	oc cluster up \
		--image=$(OC_SOURCE) \
		--version=$(OC_VERSION) \
		--host-data-dir=$(OC_DATA_DIR) \
		--use-existing-config=true
	sleep 60

oc-up-all: oc-up oc-create-rbac

oc-up-db: oc-up oc-create-db

docker-grype:
	@docker-compose build >/dev/null 2>&1

	@echo ""
	@docker run --rm \
		--volume /var/run/docker.sock:/var/run/docker.sock \
		--name Grype anchore/grype:latest \
		$$(docker images --format '{{.Repository}}' |grep rbac-server) --only-fixed

docker-up:
	@docker network ls --format '{{.Name}}' |grep -q  rbac-network > /dev/null 2>&1 && echo "" || docker network create rbac-network
	docker-compose up --build -d

docker-logs:
	docker-compose logs -f

docker-shell:
	docker-compose run --service-ports server

docker-down:
	@docker ps --format '{{.Names}}' |grep -q  rbac >/dev/null 2>&1 && docker-compose down || echo ""
	@docker network ls --format '{{.Name}}' |grep -q  rbac-network > /dev/null 2>&1 && \docker network rm rbac-network > /dev/null 2>&1 || echo ""

generate_v2_spec:
	cd docs/source/specs/typespec/ && npm ci --silent && ./compile_tsp_spec

.PHONY: docs
