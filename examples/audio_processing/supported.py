from examples.audio_processing.mel import AudioToMelSpectrogramPreprocessor
from examples.audio_processing.mfcc import AudioToMFCCPreprocessor

supported = {
    "mel": AudioToMelSpectrogramPreprocessor,
    "mfcc": AudioToMFCCPreprocessor
}