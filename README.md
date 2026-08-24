# Healthcare Claims Analytics Pipeline

A pipeline for generating, processing, and analyzing healthcare claims data.

## Project structure

```
.
├── data_generation/   # Synthetic claims data generators
├── pipeline/          # ETL / transformation logic
├── app/               # Analytics dashboard / application layer
├── sql/               # SQL schema and analytical queries
├── requirements.txt   # Python dependencies
├── .env.example       # Template for environment variables
└── .gitignore
```

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in real values
```

## Usage

_TODO: document how to run data generation, the pipeline, and the app._
