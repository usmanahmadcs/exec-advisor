PY ?= python3

.PHONY: install samples seed run test demo docker

install:
	$(PY) -m pip install -r requirements-dev.txt

samples:
	$(PY) scripts/make_sample_docs.py

seed:
	$(PY) scripts/seed_demo.py --reset

run:
	$(PY) -m advisor.app

test:
	$(PY) -m unittest discover -s tests -t . -v

demo: seed run

docker:
	docker compose up --build -d
	docker compose exec advisor python scripts/seed_demo.py
