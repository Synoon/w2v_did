"""Arabic Speech Corpus"""

from __future__ import absolute_import, division, print_function

import os

import datasets
import soundfile as sf
import random

_CITATION = """
"""

_DESCRIPTION = """\


```python
import soundfile as sf

def map_to_array(batch):
    speech_array, _ = sf.read(batch["file"])
    batch["speech"] = speech_array
    return batch

dataset = dataset.map(map_to_array, remove_columns=["file"])
```
"""

import soundfile as sf


class ComVoiceSpeechCorpus5Config(datasets.BuilderConfig):
    """BuilderConfig for DialectSpeechCorpusCorpus."""

    def __init__(self, **kwargs):
        """
        Args:
          data_dir: `string`, the path to the folder containing the files in the
            downloaded .tar
          citation: `string`, citation for the data set
          url: `string`, url for information about the data set
          **kwargs: keyword arguments forwarded to super.
        """
        super(ComVoiceSpeechCorpus5Config, self).__init__(version=datasets.Version("2.1.0", ""), **kwargs)


def map_to_array(batch):
    start, stop = batch['segment'].split('_')
    speech_array, _ = sf.read(batch["file"], start=start, stop=stop)
    batch["speech"] = speech_array
    return batch


class ComVoiceSpeechCorpus5(datasets.GeneratorBasedBuilder):
    """DialectSpeechCorpus dataset."""

    BUILDER_CONFIGS = [
        ComVoiceSpeechCorpus5Config(name="clean", description="'Clean' speech."),
    ]

    def _info(self):
        return datasets.DatasetInfo(
            description=_DESCRIPTION,
            features=datasets.Features(
                {
                    "file": datasets.Value("string"),
                    "label": datasets.features.ClassLabel(
                        names=[
                            'NLD',
                            'ESP',
                            'ITA',
                            'CHE',
                            'RUS'
                        ]
                    )
                }
            ),
            supervised_keys=("file", "text"),
            citation=_CITATION,
        )

    def _split_generators(self, dl_manager):
        archive_path = '/cluster/home/fiviapas/data_LID/'
        return [
            datasets.SplitGenerator(name="train",
                                    gen_kwargs={"archive_path": os.path.join(archive_path, "multiling-train")}),
            datasets.SplitGenerator(name="test",
                                    gen_kwargs={"archive_path": os.path.join(archive_path, "multiling-val")}),
        ]

    def _generate_examples(self, archive_path):
        """Generate examples from a Librispeech archive_path."""
        wav_dir = archive_path

        paths = []
        labls = []

        for _, c in enumerate(os.listdir(wav_dir)):
            if os.path.isdir(f'{wav_dir}/{c}/'):
                for file in os.listdir(f'{wav_dir}/{c}/')[:4000]:
                    if file.endswith('.mp3'):
                        wav_path = f'{wav_dir}/{c}/{file}'
                        paths.append(wav_path)
                        labls.append(c)

        data = list(zip(paths, labls))
        random.Random(4).shuffle(data)
        paths, labls = zip(*data)
        for i in range(len(paths)):
            example = {
                "file": paths[i],
                "label": labls[i]
            }
            yield str(i), example
