# Image for agent sandboxes. Agents can also `pip install --user` packages themselves;
# add packages here (and run `make sandbox-image`) to make them available everywhere.
FROM python:3.13-slim
RUN pip install --no-cache-dir numpy scipy pandas scikit-learn statsmodels matplotlib sympy

# Tectonic: a self-contained LaTeX engine that also runs BibTeX, used to compile manuscripts.
ARG TECTONIC=0.17.0
RUN python -c "import platform, urllib.request; arch = {'aarch64': 'aarch64', 'x86_64': 'x86_64'}[platform.machine()]; \
urllib.request.urlretrieve(f'https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic%40${TECTONIC}/tectonic-${TECTONIC}-{arch}-unknown-linux-musl.tar.gz', '/tmp/t.tgz')" \
    && tar -xzf /tmp/t.tgz -C /usr/local/bin && rm /tmp/t.tgz

RUN useradd --uid 1000 --create-home agent && mkdir /workspace && chown agent /workspace
# Tectonic downloads LaTeX packages on first use. Compile a sample once so common packages are already cached.
ENV TECTONIC_CACHE_DIR=/opt/tectonic-cache
RUN mkdir -p /opt/tectonic-cache && chown agent /opt/tectonic-cache
USER agent
COPY --chown=agent sample.tex sample.bib /tmp/sample/
RUN cd /tmp/sample && for size in 10pt 11pt 12pt; do sed "s/11pt/$size/" sample.tex > s.tex && tectonic s.tex || exit 1; done && rm -rf /tmp/sample
WORKDIR /workspace
