FROM registry.access.redhat.com/hi/python:3.12-fips-builder AS base

USER root

ENV PYTHON_VERSION=3.12 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=UTF-8 \
    LC_ALL=en_US.UTF-8 \
    LANG=en_US.UTF-8 \
    PIP_NO_CACHE_DIR=off \
    PIPENV_VENV_IN_PROJECT=1 \
    PIPENV_VERBOSITY=-1 \
    APP_ROOT=/opt/rbac \
    APP_CONFIG=/opt/rbac/rbac/gunicorn.py \
    APP_HOME=/opt/rbac/rbac \
    APP_MODULE=rbac.wsgi \
    APP_NAMESPACE=rbac \
    PLATFORM="el8"


ENV SUMMARY="Insights RBAC is a role based access control web server" \
    DESCRIPTION="Insights RBAC is a role based access control web server"

LABEL summary="$SUMMARY" \
      description="$DESCRIPTION" \
      io.k8s.description="$DESCRIPTION" \
      io.k8s.display-name="insights-rbac" \
      io.openshift.expose-services="8080:http" \
      io.openshift.tags="python,python312,rh-python312" \
      com.redhat.component="python312-docker" \
      name="insights-rbac" \
      version="1" \
      maintainer="Red Hat Insights" \
      distribution-scope="private" \
      release="1" \
      url="https://github.com/project-kessel/insights-rbac" \
      vendor="Red Hat, Inc."


# Very minimal set of packages
# glibc-langpack-en is needed to set locale to en_US and disable warning about it
# gcc to compile some python packages (e.g. ciso8601)
# postgresql-devel for psycopg2, libffi-devel for cffi
RUN INSTALL_PKGS="glibc-langpack-en postgresql-server-devel postgresql gcc libffi-devel python3.12-devel curl" && \
    microdnf --nodocs -y upgrade && \
    microdnf -y --setopt=tsflags=nodocs --setopt=install_weak_deps=0 install $INSTALL_PKGS && \
    rpm -V $INSTALL_PKGS && \
    microdnf -y clean all --enablerepo='*'

# PIPENV_DEV is set to true in the docker-compose allowing
# local builds to install the dev dependencies
ARG PIPENV_DEV=False
ARG USER_ID=1001
ARG GROUP_ID=1001

# Create a Python virtual environment for use by any application to avoid
# potential conflicts with Python packages preinstalled in the main Python
# installation.
RUN python3.12 -m venv /pipenv-venv
ENV PATH="/pipenv-venv/bin:$PATH"
# Install pipenv into the virtual env
RUN \
    pip install --upgrade "pip>=26.1.2" && \
    pip install pipenv

WORKDIR ${APP_ROOT}

# install dependencies
ENV PIP_DEFAULT_TIMEOUT=100
COPY Pipfile .
COPY Pipfile.lock .
ENV PG_CONFIG=/usr/bin/pg_config
RUN \
    # install the dependencies into the working dir (i.e. ${APP_ROOT}/.venv)
    pipenv install --deploy && \
    # delete the pipenv cache
    pipenv --clear


# Runtime env variables:
ENV VIRTUAL_ENV=${APP_ROOT}/.venv
ENV \
    # Add the rbac virtual env bin to the front of PATH.
    # This activates the virtual env for all subsequent python calls.
    PATH="$VIRTUAL_ENV/bin:$PATH" \
    PROMETHEUS_MULTIPROC_DIR=/tmp \
    LOG_DIRECTORY=/tmp

# copy the src files into the workdir
COPY . .

# Copy license to /licenses for Red Hat certification
RUN mkdir -p /licenses && cp LICENSE /licenses/

# unleash cache dir
RUN mkdir -p /tmp/unleash_cache && chmod -R 777 /tmp/unleash_cache

# create the rbac group and user with non-root group privileges
RUN \
    groupadd -g ${GROUP_ID} rbac && \
    adduser rbac -u ${USER_ID} -g ${GROUP_ID} && \
    chown -R ${USER_ID}:${GROUP_ID} ${APP_ROOT} ${APP_HOME} /tmp/unleash_cache && \
    chmod -R ug+rwX ${APP_ROOT} ${APP_HOME} ${APP_HOME}/static /tmp
USER ${USER_ID}:${GROUP_ID}


# create the static files
RUN \
    python rbac/manage.py collectstatic --noinput && \
    # Remove the app.log file created during collectstatic.
    # The application will create a new one in /tmp at runtime.
    rm -f ${APP_HOME}/app.log && \
    rm -f /tmp/counter* /tmp/app.log
EXPOSE 8080

# GIT_COMMIT is added during build in `build_deploy.sh`
# Set this at the end to leverage build caching
ARG GIT_COMMIT=undefined
ENV GIT_COMMIT=${GIT_COMMIT}

# Runtime stage
FROM registry.access.redhat.com/hi/core-runtime:2.42-openssl-fips

ENV APP_ROOT=/opt/rbac \
    APP_HOME=/opt/rbac/rbac \
    APP_CONFIG=/opt/rbac/rbac/gunicorn.py \
    APP_MODULE=rbac.wsgi \
    APP_NAMESPACE=rbac \
    VIRTUAL_ENV=/opt/rbac/.venv \
    PATH="/opt/rbac/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=UTF-8 \
    LC_ALL=C.UTF-8 \
    LANG=C.UTF-8 \
    PROMETHEUS_MULTIPROC_DIR=/tmp \
    LOG_DIRECTORY=/tmp

WORKDIR ${APP_ROOT}

# Python binaries (not included in core-runtime)
COPY --from=base /usr/bin/python3 /usr/bin/python3.12 /usr/bin/
COPY --from=base /usr/lib64/ /usr/lib64/
COPY --from=base /opt/rbac /opt/rbac
COPY --from=base /licenses /licenses

USER 1001

ENTRYPOINT ["./scripts/entrypoint.sh"]
