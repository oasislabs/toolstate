#!/usr/bin/env gmake

.PHONY: lint

lint:
	python3 -m black --diff *.py
	python3 -m pylint *.py
