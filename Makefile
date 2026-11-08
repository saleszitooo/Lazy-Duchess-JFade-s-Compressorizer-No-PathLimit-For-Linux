CC ?= cc
CFLAGS ?= -O2 -Wall -Wextra

all: dbpf-recompress

dbpf-recompress: dbpf-recompress.c
	$(CC) $(CFLAGS) -o $@ $<

test: all
	python3 -I tests/test_roundtrip.py
	python3 -I tests/test_gui_logic.py

clean:
	rm -f dbpf-recompress

.PHONY: all test clean
