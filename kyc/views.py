"""Public API views for identity verification (KYC).

Admin review (list + approve/reject) lives in adminpanel/views.py,
alongside the rest of the admin panel's endpoints — same split as crypto's
public views.py vs admin-only endpoints in adminpanel.
"""

from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from . import services
from .serializers import KycVerificationSerializer, SubmitVerificationSerializer


def _error_response(exc: services.KycServiceError) -> Response:
    return Response({'detail': exc.message, 'code': exc.code}, status=exc.status)


class SubmitVerificationView(APIView):
    """POST /api/kyc/submit/ — multipart { id_type, id_number, selfie, id_document?, address? }

    Calls Dojah synchronously and returns the resulting status; approved
    and rejected are final for the auto-decided range, review sits in the
    admin queue until a human acts on it.
    """

    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        serializer = SubmitVerificationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            verification = services.submit_verification(
                request.user,
                id_type=data['id_type'],
                id_number=data['id_number'],
                selfie_file=data['selfie'],
                id_document_file=data.get('id_document'),
                address=data.get('address', ''),
            )
        except services.KycServiceError as exc:
            return _error_response(exc)

        return Response(KycVerificationSerializer(verification).data)


class VerificationStatusView(APIView):
    """GET /api/kyc/status/ — current user's verification status, if any."""

    def get(self, request):
        verification = services.get_verification(request.user)
        if verification is None:
            return Response({'status': None})
        return Response(KycVerificationSerializer(verification).data)
