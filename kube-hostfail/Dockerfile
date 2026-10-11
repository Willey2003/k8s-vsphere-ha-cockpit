FROM python:3.12-slim
LABEL org.opencontainers.image.source="https://github.com/Willey2003/k8s-vsphere-ha-cockpit" \
      org.opencontainers.image.licenses="Apache-2.0"
WORKDIR /app
COPY pyproject.toml README.md ./
COPY hostfail ./hostfail
COPY examples ./examples
RUN pip install --no-cache-dir . && useradd -u 10001 -M hostfail
USER 10001
EXPOSE 9877
ENTRYPOINT ["kube-hostfail"]
CMD ["serve", "--port", "9877", "--interval", "60"]
