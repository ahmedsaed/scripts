FROM hexparrot/mineos:latest

RUN apt-get update && \
    apt-get install -y wget gnupg && \
    wget -O- https://packages.adoptium.net/artifactory/api/gpg/key/public | gpg --dearmor > /usr/share/keyrings/adoptium.gpg && \
    echo "deb [signed-by=/usr/share/keyrings/adoptium.gpg] https://packages.adoptium.net/artifactory/deb bookworm main" > /etc/apt/sources.list.d/adoptium.list && \
    apt-get update && \
    apt-get install -y temurin-21-jdk && \
    update-alternatives --install /usr/bin/java java /usr/lib/jvm/temurin-21-jdk-amd64/bin/java 1 && \
    update-alternatives --set java /usr/lib/jvm/temurin-21-jdk-amd64/bin/java && \
    java -version && \
    apt-get clean && rm -rf /var/lib/apt/lists/*
