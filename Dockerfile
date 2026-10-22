FROM pyd4vinci/scrapling:latest

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config
RUN pip install --no-cache-dir . && offerscan install-browser --with-deps

VOLUME ["/app/data", "/app/reports"]
ENTRYPOINT ["offerscan"]
CMD ["run"]
