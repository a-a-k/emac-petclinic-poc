# syntax=docker/dockerfile:1.7
ARG MAVEN_IMAGE
ARG JAVA_IMAGE
FROM ${MAVEN_IMAGE} AS build
WORKDIR /source
COPY vendor/application-signals-demo/ ./
RUN mvn --batch-mode -DskipTests -Dcheckstyle.skip=true package
COPY experiment/runtime-support/ /runtime-support/
RUN mvn --batch-mode -f /runtime-support/pom.xml package

FROM alpine:3.20.3@sha256:1e42bbe2508154c9126d48c2b8a75420c3544343bf86fd041fb7527e017a4b4a AS agent
ARG OTEL_AGENT_VERSION=2.11.0
ARG OTEL_AGENT_SHA256=4cff4ab46179260a61fc0d884f3f170cfbd9d2962dd260be2cff31262d0c7618
RUN wget -q -O /opentelemetry-javaagent.jar \
      "https://github.com/open-telemetry/opentelemetry-java-instrumentation/releases/download/v${OTEL_AGENT_VERSION}/opentelemetry-javaagent.jar" \
    && echo "${OTEL_AGENT_SHA256}  /opentelemetry-javaagent.jar" | sha256sum -c -

FROM ${JAVA_IMAGE}
ARG MODULE
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /source/${MODULE}/target/${MODULE}-2.6.7.jar /application.jar
COPY --from=build /runtime-support/target/emac-petclinic-runtime-support-1.0.0.jar /experiment-support/
COPY --from=agent /opentelemetry-javaagent.jar /otel/opentelemetry-javaagent.jar
EXPOSE 8080
ENTRYPOINT ["java", "-Dloader.path=/experiment-support/", "-cp", "/application.jar", "org.springframework.boot.loader.PropertiesLauncher"]
