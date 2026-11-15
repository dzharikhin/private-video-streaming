IMAGE ?= ghcr.io/dzharikhin/private-video-streaming
TAG  ?= local

.PHONY: test image smoke run

test:
	python3 -m unittest discover -p 'test_*.py' -v

image:
	docker build -t $(IMAGE):$(TAG) .

smoke: image
	IMAGE=$(IMAGE):$(TAG) ./scripts/smoke-test.sh

run: image
	docker run --rm -it --name private-video-streaming \
	  -p 8080:8080 --device /dev/video0 \
	  -e AUTH_USER=me -e AUTH_PASS=secret \
	  $(IMAGE):$(TAG)
