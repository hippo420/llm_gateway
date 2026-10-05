"""OpenAI-compatible embeddings endpoint contract tests."""

import pytest


@pytest.mark.parametrize("inputs", ["hello", ["hello", "world"]])
async def test_embeddings_returns_openai_shape(client, fake_adapter, inputs):
    response = await client.post(
        "/v1/embeddings",
        json={"model": "qwen-7b", "input": inputs},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "list"
    assert body["model"] == "qwen-7b"
    assert [item["index"] for item in body["data"]] == list(range(len(body["data"])))
    assert all(item["object"] == "embedding" for item in body["data"])
    assert all(item["embedding"] == [0.1, 0.2] for item in body["data"])
    assert body["usage"] == {"prompt_tokens": 4, "total_tokens": 4}
    assert response.headers["X-Gateway-Deployment"] == "qwen-7b@fake"
    assert fake_adapter.embedding_calls[0].model == "qwen2.5:7b"


async def test_embeddings_reject_empty_input(client):
    response = await client.post("/v1/embeddings", json={"model": "qwen-7b", "input": []})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "GW-4000"


async def test_embeddings_unknown_model_returns_model_not_found(client):
    response = await client.post(
        "/v1/embeddings", json={"model": "missing", "input": "hello"}
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "GW-4001"