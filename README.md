# Skybot (will rename it when find something).

## Goals
* simplicity
  * little boilerplate
  *  minimal magic
* power
  * multithreading
  * automatic reloading
  * extensibility

# Features
* Multithreaded dispatch and the ability to connect to multiple networks at a time.
* Easy plugin development with automatic reloading and a simple hooking API.

# Requirements
To install dependencies, run:

    pip install -r requirements.txt

## Database

Skybot defaults to SQLite (stored under `persist/`).

To use PostgreSQL instead, add this to `config.json`:

    "database": {
      "type": "postgres",
      "postgres": {
        "dsn": "postgresql://USER:PASSWORD@HOST:5432/DBNAME",
        "schema_prefix": "skybot"
      }
    }

Install the driver:

    pip install "psycopg[binary]"

Skybot runs on Python 2.7, 3.7 and Python 3.13.(WIP in some areas to full update code to 3.13, for now partial support.)

## License
Skybot is public domain. If you find a way to make money using it, I'll be very impressed.

See LICENSE for precise terms.
