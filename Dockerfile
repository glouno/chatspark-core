FROM ghcr.io/astral-sh/uv:0.12.3@sha256:2d890623d310b57771ce840f0da5eed5fc6d657da05ffaa45d82797b53fa3abc AS uv
FROM python:3.11.17-slim-trixie@sha256:45037981b62b34b44602584fccbc4d884d5f7dc92c7ee86bb38a698a79fe1e51 AS native-builder
RUN apt-get update && apt-get install -y --no-install-recommends gcc make libc6-dev pkg-config curl xz-utils ca-certificates
COPY native-sources.json /tmp/native-sources.json
RUN curl -fsSL https://download.gnome.org/sources/libxml2/2.15/libxml2-2.15.4.tar.xz -o /tmp/libxml2-2.15.4.tar.xz && echo '98087fd181d9070724f3fbc65c7377db03038eb92bd882374daff44940138821  /tmp/libxml2-2.15.4.tar.xz' | sha256sum -c - && tar xf /tmp/libxml2-2.15.4.tar.xz -C /tmp && cd /tmp/libxml2-2.15.4 && PKG_CONFIG_PATH=/usr/local/lib/pkgconfig ./configure --prefix=/usr/local --without-python --without-iconv --without-zlib --without-lzma && make -j2 && make install && ldconfig && mkdir -p /native-notices/libxml2 && cp -a Copyright /native-notices/libxml2/
RUN curl -fsSL https://download.gnome.org/sources/libxslt/1.1/libxslt-1.1.45.tar.xz -o /tmp/libxslt-1.1.45.tar.xz && echo '9acfe68419c4d06a45c550321b3212762d92f41465062ca4ea19e632ee5d216e  /tmp/libxslt-1.1.45.tar.xz' | sha256sum -c - && tar xf /tmp/libxslt-1.1.45.tar.xz -C /tmp && cd /tmp/libxslt-1.1.45 && PKG_CONFIG_PATH=/usr/local/lib/pkgconfig ./configure --prefix=/usr/local --without-python --without-crypto && make -j2 && make install && ldconfig && mkdir -p /native-notices/libxslt && cp -a Copyright /native-notices/libxslt/
RUN curl -fsSL https://sqlite.org/2026/sqlite-autoconf-3530400.tar.gz -o /tmp/sqlite-autoconf-3530400.tar.gz && echo '0e9483900e92cd5de8fd48d16bf9200145a61f7fd5be542a5ac81d8a9516eb9c  /tmp/sqlite-autoconf-3530400.tar.gz' | sha256sum -c - && tar xf /tmp/sqlite-autoconf-3530400.tar.gz -C /tmp && cd /tmp/sqlite-autoconf-3530400 && PKG_CONFIG_PATH=/usr/local/lib/pkgconfig ./configure --prefix=/usr/local --enable-fts5 --disable-readline && make -j2 && make install && ldconfig && mkdir -p /native-notices/sqlite && sed -n '1,30p' sqlite3.c > /native-notices/sqlite/PUBLIC-DOMAIN.txt
FROM native-builder AS lxml-builder
RUN apt-get update && apt-get install -y --no-install-recommends zlib1g-dev=1:1.3.dfsg+really1.3.1-1+b1
COPY --from=uv /uv /usr/local/bin/uv

COPY requirements-lxml-build.lock requirements-lxml-source.lock /tmp/
RUN uv pip install --system --no-cache --require-hashes -r /tmp/requirements-lxml-build.lock
RUN STATIC_DEPS=false python -m pip wheel --no-deps --no-build-isolation --no-binary=lxml --require-hashes -r /tmp/requirements-lxml-source.lock --wheel-dir /wheels
FROM python:3.11.17-slim-trixie@sha256:45037981b62b34b44602584fccbc4d884d5f7dc92c7ee86bb38a698a79fe1e51 AS numpy-builder
COPY --from=uv /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends gcc g++ gfortran=4:14.2.0-1 libc6-dev pkg-config=1.8.1-4 ninja-build=1.12.1-1 libblas-dev=3.12.1-6 liblapack-dev=3.12.1-6
COPY requirements-numpy-build.lock requirements-numpy-source.lock /tmp/
RUN uv pip install --system --no-cache --require-hashes -r /tmp/requirements-numpy-build.lock
RUN python -m pip wheel --no-deps --no-build-isolation --no-binary=numpy --require-hashes -r /tmp/requirements-numpy-source.lock --config-settings=setup-args=-Dblas=blas --config-settings=setup-args=-Dlapack=lapack --config-settings=setup-args=-Dallow-noblas=false --config-settings=compile-args=-j2 --wheel-dir=/wheels
FROM python:3.11.17-slim-trixie@sha256:45037981b62b34b44602584fccbc4d884d5f7dc92c7ee86bb38a698a79fe1e51 AS prepared-runtime
ARG ENGINE_REVISION
LABEL org.opencontainers.image.revision="${ENGINE_REVISION}" org.opencontainers.image.licenses="Elastic-2.0"
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libssl3t64=3.5.7-1~deb13u3 openssl=3.5.7-1~deb13u3 libpcre2-8-0=10.46-1~deb13u3 libblas3=3.12.1-6 liblapack3=3.12.1-6 libgfortran5=14.2.0-19 libquadmath0=14.2.0-19 && rm -rf /var/lib/apt/lists/*
COPY --from=native-builder /usr/local/lib/libxml2.so* /usr/local/lib/
COPY --from=native-builder /usr/local/lib/libxslt.so* /usr/local/lib/
COPY --from=native-builder /usr/local/lib/libexslt.so* /usr/local/lib/
COPY --from=native-builder /usr/local/lib/libsqlite3.so* /usr/local/lib/
COPY --from=native-builder /native-notices /usr/local/share/chatspark/native-notices
COPY native-sources.json /usr/local/share/chatspark/native-sources.json
RUN ldconfig
COPY requirements-core.lock ./
COPY --from=lxml-builder /wheels /tmp/lxml-wheels
RUN uv pip install --system --no-cache --no-deps /tmp/lxml-wheels/*.whl && rm -rf /tmp/lxml-wheels
COPY --from=numpy-builder /wheels /tmp/numpy-wheels
RUN uv pip install --system --no-cache --no-deps /tmp/numpy-wheels/*.whl && rm -rf /tmp/numpy-wheels
RUN uv pip install --system --no-cache --no-deps --require-hashes -r requirements-core.lock && uv pip check --system
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY chatspark ./chatspark
COPY scripts/prepare_image.py /tmp/prepare_image.py
RUN uv pip install --system --no-cache --no-deps . && python /tmp/prepare_image.py && uv pip uninstall --system pip setuptools wheel && rm -f /tmp/prepare_image.py && rm -rf /root/.cache /app/chatspark_core.egg-info /app/build /usr/local/bin/uv
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 CHATSPARK_STATE_ROOT=/state
RUN mkdir /state && chown 10001:10001 /state
# Export only the reviewed final filesystem. Removed dependency resources and
# installation wheels must not remain recoverable in intermediate image layers.
FROM scratch
COPY --from=prepared-runtime / /
ARG ENGINE_REVISION
LABEL org.opencontainers.image.revision="${ENGINE_REVISION}" org.opencontainers.image.licenses="Elastic-2.0"
ENV PATH=/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 CHATSPARK_STATE_ROOT=/state
WORKDIR /app
USER 10001:10001
CMD ["uvicorn", "chatspark.serve.api:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
