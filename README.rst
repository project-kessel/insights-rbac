=========================================
Insights Role Based Access Control README
=========================================

|license| |Build Status| |Docs|

~~~~~
About
~~~~~

Insights RBAC's goal is to provide an open source solution for storing roles, permissions and groups.

Full documentation is available through readthedocs_.
More info is available through platformdocs_.


Getting Started
===============

This is a Python project developed using Python 3.12. Make sure you have at least this version installed.

Additionally, the development environment installation requires the postgresql-devel package installed for your distribution before running properly.

Development
===========

To get started developing against Insights-rbac first clone a local copy of the git repository. ::

    git clone https://github.com/project-kessel/insights-rbac.git

Developing inside a virtual environment is recommended. A Pipfile is provided. Pipenv is recommended for combining virtual environment (virtualenv) and dependency management (pip). To install pipenv, use pip ::

    pip3 install pipenv

Then project dependencies and a virtual environment can be created using ::

    pipenv install --dev

To activate the virtual environment run ::

    pipenv shell

Preferred Environment
---------------------

Please refer to `Working with Openshift`_.

Ephemeral Environment
---------------------
For Red Hatters looking to run RBAC in the ephemeral environment, see `insights-service-deployer`_.

Alternative Environment
-----------------------
If deploying with Openshift seems overly complex you can try an alternate local environment where you will need to install and setup some of the dependencies and configuration.

Configuration
^^^^^^^^^^^^^

This project is developed using the Django web framework. Many configuration settings can be read in from a `.env` file. An example file `.env.example` is provided in the repository. To use the defaults simply ::

    cp .env.example .env


Modify as you see fit.

Database
^^^^^^^^

PostgreSQL is used as the database backend for Insights-rbac. A docker-compose file is provided for creating a local database container. The scripts automatically detect and support both Docker and Podman as container runtimes.

If modifications were made to the .env file the docker-compose file will need to be modified to ensure matching database credentials. Several commands are available for interacting with the database. ::

    # This will launch a Postgres container
    make start-db

    # This will run Django's migrations against the database
    make run-migrations

    # This will stop and remove a currently running database and run the above commands
    make reinitdb

Assuming the default .env file values are used, to access the database directly using psql run ::

    psql postgres -U postgres -h localhost -p 15432

There is a known limitation with docker-compose and Linux environments with SELinux enabled. You may see the following error during the postgres container deployment::

    "mkdir: cannot create directory '/var/lib/pgsql/data/userdata': Permission denied" can be resolved by granting ./pg_data ownership permissions to uid:26 (postgres user in centos/postgresql-96-centos7)

If a container running Postgres is not feasible, it is possible to run Postgres locally as documented in the Postgres tutorial_. The default port for local Postgres installations is `5432`. Make sure to modify the `.env` file accordingly. To initialize the database run ::

    make run-migrations

You may also run migrations explicitly, and in parallel, by specifying `TENANT_PARALLEL_MIGRATION_MAX_PROCESSES` (the number of concurrent processes to run migrations) and/or `TENANT_PARALLEL_MIGRATION_CHUNKS` (the number of migrations for each process to run at a time). Both of these values default to 2. *Be mindful of the fact that bumping these values will consume more database connections:*

    TENANT_PARALLEL_MIGRATION_MAX_PROCESSES=4 TENANT_PARALLEL_MIGRATION_CHUNKS=2 ./rbac/manage.py migrate

Seeds
^^^^^

Default roles and groups are automatically seeded when the application starts by default unless either of the following environment variables are set to 'False' respectively: ::

  PERMISSION_SEEDING_ENABLED
  ROLE_SEEDING_ENABLED
  GROUP_SEEDING_ENABLED

Locally these are sourced from `/rbac/management/role/definitions/*.json`, while the config maps in deployed instances are source from our `RBAC config repo`_. **If any changes to default roles/groups are required, they should be make there.**

You can also execute the following Django command to run seeds manually. It's recommended that you disable db signals while running seeds with `ACCESS_CACHE_CONNECT_SIGNALS=False`. Caching will be busted after seeding for each tenant has processed. You may also specify the number of concurrent threads in which seeds should be run, by setting `MAX_SEED_THREADS` either in the process, or the app environment. The default value is 2. *Be mindful of the fact that bumping this value will consume more database connections:* ::

  ACCESS_CACHE_CONNECT_SIGNALS=False MAX_SEED_THREADS=2 ./rbac/manage.py seeds [--roles|--groups|--permissions]

Server
^^^^^^

To run a local dev Django server you can use ::

    make serve

To run the local dev Django on a specific port use::

    make PORT=8111 serve

Migrating Relations
^^^^^^^^^^^^^^^^^^^

To run the migrator tool to convert RBAC data into [Kessel relations](https://github.com/project-kessel/relations-api), use ::

    DJANGO_READ_DOT_ENV_FILE=True ./rbac/manage.py migrate_relations [--org-list ORG_LIST [ORG_LIST ...]] [--exclude-apps EXCLUDE_APPS [EXCLUDE_APPS ...]] [--write-to-db]

Kafka and Debezium Change Data Capture Setup
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

The Debezium setup script provides a complete automated solution for setting up Change Data Capture (CDC) with Kafka for RBAC event streaming. This enables real-time replication of RBAC operations to external systems.

**Container Runtime Support**

The scripts automatically detect and support both Docker and Podman as container runtimes. The scripts will use whichever is available and running on your system.

**Quick Setup (Recommended)**

To set up the complete Debezium infrastructure automatically: ::

    ./scripts/debezium/setup_debezium.sh

This script will:

1. Create required container networks
2. Start and configure PostgreSQL with logical replication
3. Launch Kafka, Zookeeper, Kafka Connect, and Kafdrop services
4. Create and configure the Debezium PostgreSQL connector
5. Set up Kafka topics and consumer groups
6. Verify the complete setup

**Running the Kafka Consumer**

After setup, you can run the RBAC Kafka consumer to process replication events:

For interactive mode (shows logs in real-time): ::

    ./scripts/debezium/setup_debezium.sh --consumer

For background mode: ::

    ./scripts/debezium/setup_debezium.sh --consumer-bg

For a custom topic: ::

    ./scripts/debezium/setup_debezium.sh --consumer my-custom-topic

**Testing the Setup**

Send a test replication message: ::

    ./scripts/debezium/send_test_relations_message.sh

**Monitoring and Management**

* **Kafdrop UI**: http://localhost:9001 - Browse Kafka topics and messages
* **Kafka Connect API**: http://localhost:8083 - Monitor connectors and status
* **PostgreSQL**: localhost:15432 - Direct database access

**Useful Commands**

Check connector status: ::

    curl http://localhost:8083/connectors/rbac-postgres-connector/status

List Kafka topics (Docker): ::

    docker exec insights_rbac-kafka-1 kafka-topics --bootstrap-server localhost:9092 --list

List Kafka topics (Podman): ::

    podman exec insights_rbac-kafka-1 kafka-topics --bootstrap-server localhost:9092 --list

View messages in the replication topic (Docker): ::

    docker exec insights_rbac-kafka-1 kafka-console-consumer --bootstrap-server localhost:9092 --topic outbox.event.relations-replication-event --from-beginning --timeout-ms 5000

View messages in the replication topic (Podman): ::

    podman exec insights_rbac-kafka-1 kafka-console-consumer --bootstrap-server localhost:9092 --topic outbox.event.relations-replication-event --from-beginning --timeout-ms 5000

Check message count in topic (Docker): ::

    docker exec insights_rbac-kafka-1 kafka-run-class kafka.tools.GetOffsetShell --broker-list localhost:9092 --topic outbox.event.relations-replication-event

Check message count in topic (Podman): ::

    podman exec insights_rbac-kafka-1 kafka-run-class kafka.tools.GetOffsetShell --broker-list localhost:9092 --topic outbox.event.relations-replication-event

Stop Debezium services (Docker): ::

    docker-compose -f docker-compose.debezium.yml down

Stop Debezium services (Podman): ::

    podman-compose -f docker-compose.debezium.yml down

**Help and Options**

View all available options: ::

    ./scripts/debezium/setup_debezium.sh --help

**Prerequisites**

* Docker/Podman and docker-compose/podman-compose installed and running
* Ports 8083, 9001, 9092, 15432 available
* docker-compose.yml and docker-compose.debezium.yml present

**Event Flow**

When RBAC operations occur (like adding users to groups), they generate outbox events that flow through:

1. PostgreSQL outbox table (management_outbox)
2. Debezium Change Data Capture
3. Kafka topic (outbox.event.relations-replication-event)
4. RBAC Kafka consumer for downstream processing


Making Requests
---------------

You can make requests to RBAC locally to mimic traffic coming from the gateway, or locally within the same cluster from another internal service.

Basic/JWT Auth with an Identity Header
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

By default, with the `DEVELOPMENT` variable set to `True`, the `dev_middleware.py` will be used.
This will ensure that a mock identity header will be set on all requests for you.
You can modify this header to add new users to your tenant by changing the `username`, create new tenants by changing the `account_number`, and toggling between admin/non-admins by flipping `is_org_admin` from `True` to `False`.

This will allow you to simulate a JWT or basic-auth request from the gateway.
It does NOT allow providing a JWT directly to RBAC, which requires a JWT issuer to be configured.
Instead, you use the `x-rh-identity` header to simulate a request from the gateway.

Service to Service Requests
^^^^^^^^^^^^^^^^^^^^^^^^^^^

RBAC also allows for service-to-service requests. These requests require a preshared-key (PSK) or a JSON Web Token (JWT), and some additional headers in order to authorize the request as an "admin". To test PSK auth locally, do the following:

First disable the local setting of the identity header in `dev_middleware.py` by [commenting this line out](https://github.com/project-kessel/insights-rbac/blob/b207668440faf8f951dec75ffef8891343b4131b/rbac/rbac/dev_middleware.py#L72)

Next, start the server with: ::

  make serve SERVICE_PSKS='{"catalog": {"secret": "abc123"}}'

This configures an acceptable PSK. Verify that you cannot access any endpoints requiring auth: ::

  curl http://localhost:8000/api/rbac/v1/roles/ -v

Verify that if you pass in the correct PSK headers/values, you *can* access the endpoint: ::

  curl http://localhost:8000/api/rbac/v1/roles/ -v -H 'x-rh-rbac-psk: abc123' -H 'x-rh-rbac-org-id: 10001' -H 'x-rh-rbac-client-id: catalog'

Change the 'x-rh-rbac-client-id', 'x-rh-rbac-psk' and 'x-rh-rbac-org-id' header values to see that you should get back a 401 (or 400 with an account that doesn't exist).

You can also send a request *with* the identity header explicitly in the curl command along with the service-to-service headers to verify that the identity header will take precedence.

Building Documentation
^^^^^^^^^^^^^^^^^^^^^^^

The project uses Sphinx to build documentation from reStructuredText files and Python docstrings.
Documentation is automatically published to `readthedocs`_ when changes are pushed.

To build documentation locally::

  $ make html
  $ open docs/_build/html/index.html

For detailed information about the documentation system, dependencies, and troubleshooting,
see `docs/BUILD_DOCS.md`_.

Generating v2 openAPI specification
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

OpenAPI v2 specification is located in `docs/source/specs/v2/openapi.yaml`.
This OpenAPI v2 specification is generated from TypeSpec file which is located in `docs/source/specs/typespec/main.tsp`

Command to generate OpenAPI v2 specification from TypeSpec file:

NOTE: TypeSpec is required, you can install it here: https://typespec.io/docs
Please install TypeSpec in `docs/source/specs/typespec/`

  $ make generate_v2_spec


Testing and Linting
-------------------

Insights-rbac uses tox to standardize the environment used when running tests. Essentially, tox manages its own virtual environment and a copy of required dependencies to run tests. To ensure a clean tox environment run ::

    tox -r

This will rebuild the tox virtual env and then run all tests.

To run unit tests specifically::

    tox -e py312

To lint the code base ::

    tox -e lint


Feature Flags
---------------
You can configure Unleash for feature flag support in RBAC. In a Clowder environment,
this should be initialized automatically if FeatureFlags are enabled in the ClowdApp.

Locally, you can configure Unleash by setting the following:

.. code-block:: bash

  FEATURE_FLAGS_TOKEN # your Unleash API token
  FEATURE_FLAGS_URL # your Unleash url, defaulting to http://localhost:4242/api
  FEATURE_FLAGS_CACHE_DIR # filesystem cache location, defaulting to '/tmp/unleash_cache'

Start a `local Unleash server <https://docs.getunleash.io/quickstart>`_.

You can enforce feature flags, including custom constraints to allow gradual rollout
to orgs by using the following pattern, assuming you've defined a context field `orgId`
in Unleash, and are using that as a constraint in a flag's strategy to set an allow list:

.. code-block:: python

    from feature_flags import FEATURE_FLAGS

    # org-specific rollout with context fields
    show_alpha_feature = FEATURE_FLAGS.is_enabled("rbac.alpha_feature", {"orgId": request.user.org_id})
    if show_alpha_feature:
        print("Awesome alpha feature!")

    # no context fields
    show_beta_feature = FEATURE_FLAGS.is_enabled("rbac.beta_feature")
    if show_beta_feature:
        print("Awesome beta feature!")

Caveats
-------

For all requests to the Insights RBAC API, it is assumed and required that principal
information for the request be sent in a header named: `x-rh-identity`. The information
in this header is used to determine the tenant, principal and other account-level
information for the request.

Consumers of this API through cloud.redhat.com should not be concerned with adding
this header, as it will be overwritten by the gateway. All traffic to the Insights
RBAC API comes through Akamai and the Insights 3scale Gateway. The gateway is responsible
for adding the `x-rh-identity` header to all authenticated requests.

Any internal, service-to-service requests which do **not** go through the gateway
will need to have this header added to each request.

This header requirement is not reflected in the openapi.json spec, as it would
cause spec-based API clients to require the header, which would be superfluously
added to all requests on cloud.redhat.com.

Contributing
=============

This repository uses `pre-commit <https://pre-commit.com>`_ to check and enforce code style. It uses
`Black <https://github.com/psf/black>`_ to reformat the Python code and `Flake8 <http://flake8.pycqa.org>`_ to check it
afterwards. Other formats and text files are linted as well.

Install pre-commit hooks to your local repository by running:

  $ pre-commit install

After that, all your committed files will be linted. If the checks don’t succeed, the commit will be rejected. Please
make sure all checks pass before submitting a pull request. Thanks!

Repositories of the roles to be seeded
--------------------------------------

Default roles can be found in the `RBAC config repo`_.

For additional information please refer to Contributing_.

.. _readthedocs: http://insights-rbac.readthedocs.io/en/latest/
.. _`docs/BUILD_DOCS.md`: docs/BUILD_DOCS.md
.. _platformdocs: https://consoledot.pages.redhat.com/docs/dev/services/rbac.html
.. _tutorial: https://www.postgresql.org/docs/10/static/tutorial-start.html
.. _`Working with Openshift`: https://insights-rbac.readthedocs.io/en/latest/openshift.html
.. _Contributing: https://insights-rbac.readthedocs.io/en/latest/CONTRIBUTING.html
.. _insights-service-deployer: https://github.com/project-kessel/insights-service-deployer

.. |license| image:: https://img.shields.io/github/license/project-kessel/insights-rbac.svg
   :target: https://github.com/project-kessel/insights-rbac/blob/master/LICENSE
.. |Build Status| image:: https://ci.ext.devshift.net/buildStatus/icon?job=RedHatInsights-insights-rbac-gh-build-master
   :target: https://ci.ext.devshift.net/job/RedHatInsights-insights-rbac-gh-build-master/
.. |Docs| image:: https://readthedocs.org/projects/insights-rbac/badge/
   :target: https://insights-rbac.readthedocs.io/en/latest/
.. _`RBAC config repo`: https://github.com/project-kessel/rbac-config.git
