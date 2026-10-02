VENV := ./venv/bin

.PHONY: install check push-checks test

install:
	python3 -m venv venv
	$(VENV)/pip install -U pip
	$(VENV)/pip install -r requirements.txt
	$(VENV)/pre-commit install

check:
	$(VENV)/pre-commit run --all-files

push-checks:
	$(VENV)/pre-commit run --all-files --hook-stage pre-push

test:
	$(VENV)/python -m unittest discover -s tests -t .
