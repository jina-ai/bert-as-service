import os

import numpy as np
import pytest
from docarray import DocumentArray, Document

from clip_client import Client
from clip_server.executors.clip_onnx import CLIPEncoder as ONNXCLILPEncoder
from clip_server.executors.clip_torch import CLIPEncoder as TorchCLIPEncoder


@pytest.mark.asyncio
@pytest.mark.parametrize('encoder_class', [TorchCLIPEncoder, ONNXCLILPEncoder])
async def test_torch_executor_rank_img2texts(encoder_class):
    ce = encoder_class()

    da = DocumentArray.from_files(
        f'{os.path.dirname(os.path.abspath(__file__))}/**/*.jpg'
    )
    for d in da:
        d.matches.append(Document(text='hello, world!'))
        d.matches.append(Document(text='goodbye, world!'))
        d.matches.append(Document(text='goodbye,!'))
        d.matches.append(Document(text='good world!'))
        d.matches.append(Document(text='good!'))
        d.matches.append(Document(text='world!'))

    await ce.rank(da, {})
    print(da['@m', 'scores__clip_score__value'])
    for d in da:
        for c in d.matches:
            assert c.scores['clip_score'].value is not None
            assert not c.tensor
        org_score = d.matches[:, 'scores__clip_score__value']
        assert org_score == list(sorted(org_score, reverse=True))
        assert not d.tensor


@pytest.mark.asyncio
@pytest.mark.parametrize('encoder_class', [TorchCLIPEncoder, ONNXCLILPEncoder])
async def test_torch_executor_rank_text2imgs(encoder_class):
    ce = encoder_class()
    db = DocumentArray(
        [Document(text='hello, world!'), Document(text='goodbye, world!')]
    )
    for d in db:
        d.matches.extend(
            DocumentArray.from_files(
                f'{os.path.dirname(os.path.abspath(__file__))}/**/*.jpg'
            )
        )
    await ce.rank(db, {})
    print(db['@m', 'scores__clip_score__value'])
    for d in db:
        for c in d.matches:
            assert c.scores['clip_score'].value is not None
            assert c.scores['clip_score_cosine'].value is not None
            assert not c.tensor
        np.testing.assert_almost_equal(
            sum(c.scores['clip_score'].value for c in d.matches), 1
        )
        assert not d.tensor
        assert not d.blob


@pytest.mark.parametrize(
    'inputs',
    [
        [
            Document(
                uri='https://clip-as-service.jina.ai/_static/favicon.png',
                matches=[
                    Document(text='hello, world'),
                    Document(text='goodbye, world'),
                ],
            ),
            Document(
                uri='https://clip-as-service.jina.ai/_static/favicon.png',
                matches=[
                    Document(text='hello, world'),
                    Document(text='goodbye, world'),
                ],
            ),
        ],
        DocumentArray(
            [
                Document(
                    uri='https://clip-as-service.jina.ai/_static/favicon.png',
                    matches=[
                        Document(text='hello, world'),
                        Document(text='goodbye, world'),
                    ],
                ),
                Document(
                    uri='https://clip-as-service.jina.ai/_static/favicon.png',
                    matches=[
                        Document(text='hello, world'),
                        Document(text='goodbye, world'),
                    ],
                ),
            ]
        ),
        lambda: (
            Document(
                uri='https://clip-as-service.jina.ai/_static/favicon.png',
                matches=[
                    Document(text='hello, world'),
                    Document(text='goodbye, world'),
                ],
            )
            for _ in range(10)
        ),
        DocumentArray(
            [
                Document(
                    text='hello, world',
                    matches=[
                        Document(
                            uri='https://clip-as-service.jina.ai/_static/favicon.png'
                        ),
                        Document(
                            uri=f'{os.path.dirname(os.path.abspath(__file__))}/img/00000.jpg'
                        ),
                    ],
                )
            ]
        ),
    ],
)
def test_docarray_inputs(make_flow, inputs):
    c = Client(server=f'grpc://0.0.0.0:{make_flow.port}')
    r = c.rank(inputs if not callable(inputs) else inputs())
    assert not r[0].tensor
    assert isinstance(r, DocumentArray)
    rv1 = r['@m', 'scores__clip_score__value']
    rv2 = r['@m', 'scores__clip_score_cosine__value']
    for v1, v2 in zip(rv1, rv2):
        assert v1 is not None
        assert v1 > 0
        assert v2 is not None
        assert v2 > 0


@pytest.mark.parametrize(
    'inputs',
    [
        [
            Document(
                uri='https://clip-as-service.jina.ai/_static/favicon.png',
                matches=[
                    Document(text='hello, world'),
                    Document(text='goodbye, world'),
                ],
            ),
        ],
        DocumentArray(
            [
                Document(
                    uri='https://clip-as-service.jina.ai/_static/favicon.png',
                    matches=[
                        Document(text='hello, world'),
                        Document(text='goodbye, world'),
                    ],
                ),
            ]
        ),
        lambda: (
            Document(
                uri='https://clip-as-service.jina.ai/_static/favicon.png',
                matches=[
                    Document(text='hello, world'),
                    Document(text='goodbye, world'),
                ],
            )
            for _ in range(1)
        ),
        DocumentArray(
            [
                Document(
                    text='hello, world',
                    matches=[
                        Document(
                            uri='https://clip-as-service.jina.ai/_static/favicon.png'
                        ),
                        Document(
                            uri=f'{os.path.dirname(os.path.abspath(__file__))}/img/00000.jpg'
                        ),
                    ],
                )
            ]
        ),
    ],
)
@pytest.mark.asyncio
async def test_async_arank(make_flow, inputs):
    c = Client(server=f'grpc://0.0.0.0:{make_flow.port}')
    r = await c.arank(inputs if not callable(inputs) else inputs())
    assert not r[0].tensor
    assert isinstance(r, DocumentArray)
    rv = r['@m', 'scores__clip_score__value']
    for v in rv:
        assert v is not None
        assert v > 0
    np.testing.assert_almost_equal(sum(rv), 1.0)

    rv = r['@m', 'scores__clip_score_cosine__value']
    for v in rv:
        assert v is not None
        assert -1.0 <= v <= 1.0


@pytest.mark.parametrize(
    'inputs',
    [
        [
            Document(
                id=str(i), text='A', matches=[Document(text='B'), Document(text='C')]
            )
            for i in range(20)
        ],
        DocumentArray(
            [
                Document(
                    id=str(i),
                    text='A',
                    matches=[Document(text='B'), Document(text='C')],
                )
                for i in range(20)
            ]
        ),
    ],
)
def test_docarray_preserve_original_order(make_flow, inputs):
    c = Client(server=f'grpc://0.0.0.0:{make_flow.port}')
    r = c.rank(inputs, batch_size=1)
    assert isinstance(r, DocumentArray)
    for i in range(len(inputs)):
        assert inputs[i] is r[i]
        assert inputs[i].id == str(i)


@pytest.mark.parametrize(
    'inputs',
    [
        [
            Document(
                id=str(i), text='A', matches=[Document(text='B'), Document(text='C')]
            )
            for i in range(20)
        ],
        DocumentArray(
            [
                Document(
                    id=str(i),
                    text='A',
                    matches=[Document(text='B'), Document(text='C')],
                )
                for i in range(20)
            ]
        ),
    ],
)
@pytest.mark.asyncio
async def test_async_docarray_preserve_original_order(make_flow, inputs):
    c = Client(server=f'grpc://0.0.0.0:{make_flow.port}')
    r = await c.arank(inputs, batch_size=1)
    assert isinstance(r, DocumentArray)
    for i in range(len(inputs)):
        assert inputs[i] is r[i]
        assert inputs[i].id == str(i)
