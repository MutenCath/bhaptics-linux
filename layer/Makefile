# Makefile — bhaptics OpenXR implicit API layer (PoC)
#  make        -> build libXrApiLayer_bhaptics.so
#  make test   -> build harness and run it
#  make clean  -> remove build artifacts

CC      ?= gcc
CFLAGS  ?= -O2 -g -std=c99 -Wall -Wextra -Werror -fPIC -Ivendor
TARGET  := libXrApiLayer_bhaptics.so
TESTBIN := test_layer

VENDOR := vendor/openxr/openxr.h \
          vendor/openxr/openxr_platform.h \
          vendor/openxr/openxr_platform_defines.h \
          vendor/openxr/openxr_loader_negotiation.h

.PHONY: all test clean

all: $(TARGET)

$(TARGET): bhaptics_xr_layer.c $(VENDOR)
	$(CC) $(CFLAGS) -shared -o $@ bhaptics_xr_layer.c

$(TESTBIN): test_layer.c $(TARGET)
	$(CC) $(CFLAGS) -o $@ test_layer.c -ldl

test: $(TESTBIN)
	./$(TESTBIN)

clean:
	rm -f $(TARGET) $(TESTBIN)
