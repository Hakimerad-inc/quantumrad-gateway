"""Storage SOP classes the gateway accepts and forwards (review C4).

The PRD describes the gateway as modality-agnostic, but the receiver and
the DICOM forwarding handler originally negotiated only CT and MR, so a
modality sending anything else was rejected at association time and the
study never entered the spool.

This module is the single source of truth for both sides:

- :class:`~mercure_gateway.receiver.Receiver` (C-STORE SCP) advertises every
  class here, so any modality can associate.
- :class:`~mercure_gateway.forwarder.handlers.dicom.DICOMHandler` (C-STORE
  SCU) requests only the classes actually present in the study it is
  forwarding, which keeps negotiation small and correct.

Context budget: DICOM allows at most 128 presentation contexts per
association, and the SCP uses one per SOP class. ``test_sop_class_budget``
fails if this list grows past the safe headroom.
"""

from __future__ import annotations

from pathlib import Path

from pydicom.uid import (
    UID,
    AdvancedBlendingPresentationStateStorage,
    ArterialPulseWaveformStorage,
    AutorefractionMeasurementsStorage,
    BasicVoiceAudioWaveformStorage,
    BlendingSoftcopyPresentationStateStorage,
    BreastTomosynthesisImageStorage,
    CardiacElectrophysiologyWaveformStorage,
    ChestCADSRStorage,
    ColonCADSRStorage,
    ColorSoftcopyPresentationStateStorage,
    CompositingPlanarMPRVolumetricPresentationStateStorage,
    Comprehensive3DSRStorage,
    ComprehensiveSRStorage,
    ComputedRadiographyImageStorage,
    ContentAssessmentResultsStorage,
    CTImageStorage,
    DeformableSpatialRegistrationStorage,
    DigitalIntraOralXRayImageStorageForPresentation,
    DigitalIntraOralXRayImageStorageForProcessing,
    DigitalMammographyXRayImageStorageForPresentation,
    DigitalMammographyXRayImageStorageForProcessing,
    DigitalXRayImageStorageForPresentation,
    DigitalXRayImageStorageForProcessing,
    ElectromyogramWaveformStorage,
    EncapsulatedCDAStorage,
    EncapsulatedMTLStorage,
    EncapsulatedOBJStorage,
    EncapsulatedPDFStorage,
    EncapsulatedSTLStorage,
    EnhancedCTImageStorage,
    EnhancedMRColorImageStorage,
    EnhancedMRImageStorage,
    EnhancedPETImageStorage,
    EnhancedSRStorage,
    EnhancedUSVolumeStorage,
    EnhancedXAImageStorage,
    EnhancedXRFImageStorage,
    ExtensibleSRStorage,
    GeneralAudioWaveformStorage,
    GenericImplantTemplateStorage,
    GrayscalePlanarMPRVolumetricPresentationStateStorage,
    GrayscaleSoftcopyPresentationStateStorage,
    HangingProtocolStorage,
    HemodynamicWaveformStorage,
    ImplantAssemblyTemplateStorage,
    ImplantTemplateGroupStorage,
    IntraocularLensCalculationsStorage,
    IntravascularOpticalCoherenceTomographyImageStorageForPresentation,
    IntravascularOpticalCoherenceTomographyImageStorageForProcessing,
    KeratometryMeasurementsStorage,
    KeyObjectSelectionDocumentStorage,
    LegacyConvertedEnhancedCTImageStorage,
    LegacyConvertedEnhancedMRImageStorage,
    LegacyConvertedEnhancedPETImageStorage,
    LensometryMeasurementsStorage,
    MacularGridThicknessAndVolumeReportStorage,
    MammographyCADSRStorage,
    MRImageStorage,
    MultipleVolumeRenderingVolumetricPresentationStateStorage,
    NuclearMedicineImageStorage,
    OphthalmicAxialMeasurementsStorage,
    OphthalmicPhotography8BitImageStorage,
    OphthalmicPhotography16BitImageStorage,
    OphthalmicTomographyImageStorage,
    OphthalmicVisualFieldStaticPerimetryMeasurementsStorage,
    ParametricMapStorage,
    PositronEmissionTomographyImageStorage,
    ProcedureLogStorage,
    PseudoColorSoftcopyPresentationStateStorage,
    RadiopharmaceuticalRadiationDoseSRStorage,
    RawDataStorage,
    RealWorldValueMappingStorage,
    RoutineScalpElectroencephalogramWaveformStorage,
    RTBeamsTreatmentRecordStorage,
    RTBrachyTreatmentRecordStorage,
    RTDoseStorage,
    RTImageStorage,
    RTIonBeamsTreatmentRecordStorage,
    RTIonPlanStorage,
    RTPlanStorage,
    RTStructureSetStorage,
    RTTreatmentSummaryRecordStorage,
    SecondaryCaptureImageStorage,
    SegmentationStorage,
    SegmentedVolumeRenderingVolumetricPresentationStateStorage,
    SpatialFiducialsStorage,
    SpatialRegistrationStorage,
    SpectaclePrescriptionReportStorage,
    StereometricRelationshipStorage,
    SubjectiveRefractionMeasurementsStorage,
    SurfaceScanMeshStorage,
    SurfaceScanPointCloudStorage,
    SurfaceSegmentationStorage,
    UltrasoundImageStorage,
    UltrasoundMultiFrameImageStorage,
    VideoEndoscopicImageStorage,
    VideoMicroscopicImageStorage,
    VideoPhotographicImageStorage,
    VisualAcuityMeasurementsStorage,
    VLEndoscopicImageStorage,
    VLMicroscopicImageStorage,
    VLPhotographicImageStorage,
    VLSlideCoordinatesMicroscopicImageStorage,
    VLWholeSlideMicroscopyImageStorage,
    VolumeRenderingVolumetricPresentationStateStorage,
    XAXRFGrayscaleSoftcopyPresentationStateStorage,
    XRay3DAngiographicImageStorage,
    XRay3DCraniofacialImageStorage,
    XRayAngiographicImageStorage,
    XRayRadiationDoseSRStorage,
    XRayRadiofluoroscopicImageStorage,
)

__all__ = ["STORAGE_SOP_CLASSES", "sop_classes_for_files"]

# Leave room for pynetdicom's own contexts (e.g. Verification) under the
# protocol limit of 128.
_MAX_CONTEXTS = 120

STORAGE_SOP_CLASSES: tuple[UID, ...] = (
    # CT
    CTImageStorage,
    EnhancedCTImageStorage,
    LegacyConvertedEnhancedCTImageStorage,
    # MR
    EnhancedMRColorImageStorage,
    EnhancedMRImageStorage,
    LegacyConvertedEnhancedMRImageStorage,
    MRImageStorage,
    # Ultrasound
    EnhancedUSVolumeStorage,
    UltrasoundImageStorage,
    UltrasoundMultiFrameImageStorage,
    # CR / DX / Mammo / Tomosynthesis
    BreastTomosynthesisImageStorage,
    ComputedRadiographyImageStorage,
    DigitalIntraOralXRayImageStorageForPresentation,
    DigitalIntraOralXRayImageStorageForProcessing,
    DigitalMammographyXRayImageStorageForPresentation,
    DigitalMammographyXRayImageStorageForProcessing,
    DigitalXRayImageStorageForPresentation,
    DigitalXRayImageStorageForProcessing,
    # Nuclear medicine / PET
    EnhancedPETImageStorage,
    LegacyConvertedEnhancedPETImageStorage,
    NuclearMedicineImageStorage,
    PositronEmissionTomographyImageStorage,
    # Secondary capture
    SecondaryCaptureImageStorage,
    # Angiography / fluoroscopy / 3D X-ray
    EnhancedXAImageStorage,
    EnhancedXRFImageStorage,
    IntravascularOpticalCoherenceTomographyImageStorageForPresentation,
    IntravascularOpticalCoherenceTomographyImageStorageForProcessing,
    XRay3DAngiographicImageStorage,
    XRay3DCraniofacialImageStorage,
    XRayAngiographicImageStorage,
    XRayRadiofluoroscopicImageStorage,
    # Visible light / video (endoscopy, microscopy, slides)
    VLEndoscopicImageStorage,
    VLMicroscopicImageStorage,
    VLPhotographicImageStorage,
    VLSlideCoordinatesMicroscopicImageStorage,
    VLWholeSlideMicroscopyImageStorage,
    VideoEndoscopicImageStorage,
    VideoMicroscopicImageStorage,
    VideoPhotographicImageStorage,
    # Ophthalmology
    AutorefractionMeasurementsStorage,
    IntraocularLensCalculationsStorage,
    KeratometryMeasurementsStorage,
    LensometryMeasurementsStorage,
    MacularGridThicknessAndVolumeReportStorage,
    OphthalmicAxialMeasurementsStorage,
    OphthalmicPhotography16BitImageStorage,
    OphthalmicPhotography8BitImageStorage,
    OphthalmicTomographyImageStorage,
    OphthalmicVisualFieldStaticPerimetryMeasurementsStorage,
    SpectaclePrescriptionReportStorage,
    SubjectiveRefractionMeasurementsStorage,
    VisualAcuityMeasurementsStorage,
    # Radiotherapy
    RTBeamsTreatmentRecordStorage,
    RTBrachyTreatmentRecordStorage,
    RTDoseStorage,
    RTImageStorage,
    RTIonBeamsTreatmentRecordStorage,
    RTIonPlanStorage,
    RTPlanStorage,
    RTStructureSetStorage,
    RTTreatmentSummaryRecordStorage,
    # Segmentation / registration / parametric maps
    DeformableSpatialRegistrationStorage,
    ParametricMapStorage,
    RawDataStorage,
    RealWorldValueMappingStorage,
    SegmentationStorage,
    SpatialFiducialsStorage,
    SpatialRegistrationStorage,
    StereometricRelationshipStorage,
    SurfaceScanMeshStorage,
    SurfaceScanPointCloudStorage,
    SurfaceSegmentationStorage,
    # Structured reporting
    ChestCADSRStorage,
    ColonCADSRStorage,
    Comprehensive3DSRStorage,
    ComprehensiveSRStorage,
    ContentAssessmentResultsStorage,
    EnhancedSRStorage,
    ExtensibleSRStorage,
    KeyObjectSelectionDocumentStorage,
    MammographyCADSRStorage,
    ProcedureLogStorage,
    RadiopharmaceuticalRadiationDoseSRStorage,
    XRayRadiationDoseSRStorage,
    # Encapsulated documents (reports)
    EncapsulatedCDAStorage,
    EncapsulatedMTLStorage,
    EncapsulatedOBJStorage,
    EncapsulatedPDFStorage,
    EncapsulatedSTLStorage,
    # Presentation states
    AdvancedBlendingPresentationStateStorage,
    BlendingSoftcopyPresentationStateStorage,
    ColorSoftcopyPresentationStateStorage,
    CompositingPlanarMPRVolumetricPresentationStateStorage,
    GrayscalePlanarMPRVolumetricPresentationStateStorage,
    GrayscaleSoftcopyPresentationStateStorage,
    MultipleVolumeRenderingVolumetricPresentationStateStorage,
    PseudoColorSoftcopyPresentationStateStorage,
    SegmentedVolumeRenderingVolumetricPresentationStateStorage,
    VolumeRenderingVolumetricPresentationStateStorage,
    XAXRFGrayscaleSoftcopyPresentationStateStorage,
    # Waveforms
    ArterialPulseWaveformStorage,
    BasicVoiceAudioWaveformStorage,
    CardiacElectrophysiologyWaveformStorage,
    ElectromyogramWaveformStorage,
    GeneralAudioWaveformStorage,
    HemodynamicWaveformStorage,
    RoutineScalpElectroencephalogramWaveformStorage,
    # Workflow / templates
    GenericImplantTemplateStorage,
    HangingProtocolStorage,
    ImplantAssemblyTemplateStorage,
    ImplantTemplateGroupStorage,
)


_KNOWN: frozenset[UID] = frozenset(STORAGE_SOP_CLASSES)


def sop_classes_for_files(paths: list[Path]) -> tuple[UID, ...]:
    """Return the distinct storage SOP classes present in *paths*.

    Used by the forwarding SCU to request exactly the contexts a study
    needs. Each file is read with ``stop_before_pixels`` so only the header
    is parsed. Files that cannot be read, or whose SOP class is not in
    :data:`STORAGE_SOP_CLASSES`, are skipped — an unrecognised class is
    still delivered when the caller falls back to the full list.
    """
    import pydicom

    found: set[UID] = set()
    for path in paths:
        try:
            ds = pydicom.dcmread(str(path), stop_before_pixels=True)
            uid = UID(str(ds.SOPClassUID))
        except Exception:
            continue
        if uid in _KNOWN:
            found.add(uid)
    return tuple(sorted(found))


assert len(STORAGE_SOP_CLASSES) <= _MAX_CONTEXTS, (
    f"{len(STORAGE_SOP_CLASSES)} SOP classes exceeds the {_MAX_CONTEXTS}-context budget"
)
