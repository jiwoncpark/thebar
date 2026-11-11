# Image for agent sandboxes. Containers run without network access, so packages
# agents need must be added here and the image rebuilt (`make sandbox-image`).
FROM python:3.13-slim
RUN pip install --no-cache-dir numpy scipy pandas scikit-learn statsmodels matplotlib sympy
RUN useradd --uid 1000 --create-home agent && mkdir /workspace && chown agent /workspace
USER agent
WORKDIR /workspace
