# Use an official Python runtime as a parent image
FROM python:3.12.9-slim-bookworm

ARG BUILD_NUMBER=dev
ENV BUILD_NUMBER=$BUILD_NUMBER
ARG BUILD_COMMIT=dev
ENV BUILD_COMMIT=$BUILD_COMMIT
ENV TZ=Europe/Berlin

# permissions and nonroot user for tightened security
RUN adduser --disabled-login nonroot
RUN mkdir -p /home/app/logs /home/app/data /home/app/static/prints /var/log/flask-app \
    && touch /var/log/flask-app/flask-app.err.log /var/log/flask-app/flask-app.out.log \
    && chown -R nonroot:nonroot /home/app /var/log/flask-app

WORKDIR /home/app

RUN if [ "$(printenv BUILD_NUMBER)" = "dev" ]; then \
        date +%y%m%d.%H%M > /home/app/build_number; \
    else \
        printenv BUILD_NUMBER > /home/app/build_number; \
    fi

# copy all the files to the container
COPY --chown=nonroot:nonroot . .

# COPY preserves source modes. Ensure the runtime thumbnail directory is
# writable after the application tree has been copied into the image.
RUN mkdir -p /home/app/static/prints \
    && chown -R nonroot:nonroot /home/app/static/prints \
    && chmod -R u+rwX /home/app/static/prints

USER nonroot

# venv
ENV VIRTUAL_ENV=/home/app/venv

# python setup
RUN python -m venv $VIRTUAL_ENV
ENV PATH="$VIRTUAL_ENV/bin:$PATH"
ENV FLASK_APP=app_custom
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.txt

# define the port number the container should expose
EXPOSE 8000

CMD ["waitress-serve", "--host=0.0.0.0", "--port=8000", "app_custom:app"]
