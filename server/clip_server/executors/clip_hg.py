import os
import warnings
from multiprocessing.pool import ThreadPool
from typing import Dict, Optional
import numpy as np
import torch
from transformers import CLIPFeatureExtractor, CLIPModel, CLIPTokenizer
from clip_server.executors.helper import (
    split_img_txt_da,
    set_rank,
)
from jina import Executor, requests, DocumentArray


class CLIPEncoder(Executor):
    def __init__(
        self,
        pretrained_model_name_or_path: str = 'openai/clip-vit-base-patch32',
        finetuned_checkpoint_path: Optional[str] = None,
        base_feature_extractor: Optional[str] = None,
        base_tokenizer_model: Optional[str] = None,
        use_default_preprocessing: bool = True,
        max_length: int = 77,
        device: str = 'cpu',
        overwrite_embeddings: bool = False,
        num_worker_preprocess: int = 4,
        minibatch_size: int = 32,
        *args,
        **kwargs,
    ):
        """
        :param pretrained_model_name_or_path: Can be either:
            - A string, the model id of a pretrained CLIP model hosted
                inside a model repo on huggingface.co, e.g.,
                'openai/clip-vit-base-patch32'
            - A path to a directory containing model weights saved, e.g.,
                ./my_model_directory/
        :param finetuned_checkpoint_path: If set, the pretrained model weights will be replaced with weights
            loading from the given checkpoint.
        :param base_feature_extractor: Base feature extractor for images.
            Defaults to ``pretrained_model_name_or_path`` if None.
        :param base_tokenizer_model: Base tokenizer model.
            Defaults to ``pretrained_model_name_or_path`` if None.
        :param use_default_preprocessing: Whether to use the `base_feature_extractor`
            on images (tensors) before encoding them. If you disable this, you must
            ensure that the images you pass in have the correct format, see the
            ``encode`` method for details.
        :param max_length: Max length argument for the tokenizer. All CLIP models
            use 77 as the max length.
        :param device: Pytorch device to put the model on, e.g. 'cpu', 'cuda',
            'cuda:1'.
        :param overwrite_embeddings: Whether to overwrite existing embeddings. By
            default docs that have embeddings already are not processed. This value
            can be overwritten if the same parameter is passed to the request.
        :param num_worker_preprocess: Number of cpu processes used in preprocessing step.
        :param minibatch_size: Default batch size for encoding, used if the
            batch size is not passed as a parameter with the request.
        """
        super().__init__(*args, **kwargs)
        self._overwrite_embeddings = overwrite_embeddings
        self._minibatch_size = minibatch_size

        self._use_default_preprocessing = use_default_preprocessing
        self._max_length = max_length

        # self.device = device
        if not device:
            self._device = 'cuda' if torch.cuda.is_available() else 'cpu'
        else:
            self._device = device

        if not self._device.startswith('cuda') and (
            'OMP_NUM_THREADS' not in os.environ
            and hasattr(self.runtime_args, 'replicas')
        ):
            replicas = getattr(self.runtime_args, 'replicas', 1)
            num_threads = max(1, torch.get_num_threads() // replicas)
            if num_threads < 2:
                warnings.warn(
                    f'Too many replicas ({replicas}) vs too few threads {num_threads} may result in '
                    f'sub-optimal performance.'
                )

                # NOTE: make sure to set the threads right after the torch import,
                # and `torch.set_num_threads` always take precedence over environment variables `OMP_NUM_THREADS`.
                # For more details, please see https://pytorch.org/docs/stable/generated/torch.set_num_threads.html
                torch.set_num_threads(max(num_threads, 1))
                torch.set_num_interop_threads(1)

        self._vision_preprocessor = CLIPFeatureExtractor.from_pretrained(
            base_feature_extractor or pretrained_model_name_or_path
        )
        self._tokenizer = CLIPTokenizer.from_pretrained(
            base_tokenizer_model or pretrained_model_name_or_path
        )
        self._model = CLIPModel.from_pretrained(pretrained_model_name_or_path)

        if finetuned_checkpoint_path:
            if finetuned_checkpoint_path.startswith(
                'https://'
            ) or finetuned_checkpoint_path.startswith('http://'):
                state_dict = torch.hub.load_state_dict_from_url(
                    finetuned_checkpoint_path, map_location='cpu', progress=True
                )
            else:
                state_dict = torch.load(finetuned_checkpoint_path, map_location='cpu')
            self._model.load_state_dict(state_dict)

        self._model.eval().to(self._device)
        self._pool = ThreadPool(processes=num_worker_preprocess)

    def _preproc_images(self, docs: 'DocumentArray'):
        with self.monitor('preprocess_images_seconds'):
            tensors_batch = []

            for d in docs:
                content = d.content

                if d.blob:
                    d.convert_blob_to_image_tensor()
                elif d.uri:
                    d.load_uri_to_image_tensor()

                tensors_batch.append(d.tensor)

                # recover content
                d.content = content

            if self._use_default_preprocessing:
                batch_data = self._vision_preprocessor(
                    images=tensors_batch,
                    return_tensors='pt',
                )
                batch_data = {k: v.to(self._device) for k, v in batch_data.items()}

            else:
                batch_data = {
                    'pixel_values': torch.tensor(
                        tensors_batch, dtype=torch.float32, device=self._device
                    )
                }

            return docs, batch_data

    def _preproc_texts(self, docs: 'DocumentArray'):
        with self.monitor('preprocess_texts_seconds'):
            data = self._tokenizer(
                docs.texts,
                max_length=self._max_length,
                padding='longest',
                truncation=True,
                return_tensors='pt',
            )
            batch_data = {k: v.to(self._device) for k, v in data.items()}
            return docs, batch_data

    @requests(on='/rank')
    async def rank(self, docs: 'DocumentArray', parameters: Dict, **kwargs):
        await self.encode(docs['@r,m'])

        set_rank(docs)

    @requests
    async def encode(self, docs: DocumentArray, **kwargs):
        """
        Encode all documents with `text` or image content using the corresponding CLIP
        encoder. Store the embeddings in the `embedding` attribute. Documents with
        existing embeddings are not processed unless `overwrite_embeddings` is set to
        True.
        :param docs: Documents sent to the encoder. The image docs must have
            ``tensor`` of the
            shape ``Height x Width x 3``. By default, the input ``tensor`` must
            be an ``ndarray`` with ``dtype=uint8`` or ``dtype=float32``.
            If you set ``use_default_preprocessing=True`` when creating this encoder,
            then the ``tensor`` arrays should have the shape ``[H, W, 3]``, and be in
            the RGB color format with ``dtype=uint8``.
            If you set ``use_default_preprocessing=False`` when creating this encoder,
            then you need to ensure that the images you pass in are already
            pre-processed. This means that they are all the same size (for batching) -
            the CLIP model was trained on images of the size ``224 x 224``, and that
            they are of the shape ``[3, H, W]``  with ``dtype=float32``. They should
            also be normalized (values between 0 and 1).
        """
        _img_da = DocumentArray()
        _txt_da = DocumentArray()
        for d in docs:
            split_img_txt_da(d, _img_da, _txt_da)

        with torch.inference_mode():
            # for image
            if _img_da:
                for minibatch, batch_data in _img_da.map_batch(
                    self._preproc_images,
                    batch_size=self._minibatch_size,
                    pool=self._pool,
                ):

                    self._encode_images(minibatch)
                    with self.monitor('encode_images_seconds'):
                        minibatch.embeddings = (
                            self._model.get_image_features(**batch_data)
                            .cpu()
                            .numpy()
                            .astype(np.float32)
                        )

            # for text
            if _txt_da:
                for minibatch, batch_data in _txt_da.map_batch(
                    self._preproc_texts,
                    batch_size=self._minibatch_size,
                    pool=self._pool,
                ):
                    with self.monitor('encode_texts_seconds'):
                        minibatch.embeddings = (
                            self._model.get_text_features(**batch_data)
                            .cpu()
                            .numpy()
                            .astype(np.float32)
                        )

        return docs
