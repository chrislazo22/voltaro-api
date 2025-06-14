import asyncio
from datetime import datetime, timezone
from loguru import logger
from ocpp.v16 import ChargePoint as ChargePointV16
from ocpp.v16 import call_result, call
from ocpp.routing import on
from app.database import AsyncSessionLocal
from app.models import (
    ChargePoint as ChargePointModel,
    IdTag,
    Session,
    MeterValue,
    ConnectorStatus,
)
from app.utils import utc_now_iso, utc_now_naive, parse_ocpp_timestamp
from sqlalchemy import select
import random


class ChargePoint(ChargePointV16):
    """
    Custom ChargePoint class for handling OCPP 1.6 messages.
    """

    @on("BootNotification")
    async def on_boot_notification(
        self, charge_point_vendor, charge_point_model, **kwargs
    ):
        logger.info(
            f"BootNotification received from {self.id}: vendor={charge_point_vendor}, model={charge_point_model}"
        )

        # Store/update charge point in database
        async with AsyncSessionLocal() as session:
            try:
                # Check if charge point already exists
                charge_point = await session.get(ChargePointModel, self.id)

                if charge_point is None:
                    # Create new charge point
                    charge_point = ChargePointModel(
                        id=self.id,
                        vendor=charge_point_vendor,
                        model=charge_point_model,
                        charge_point_serial_number=kwargs.get(
                            "charge_point_serial_number"
                        ),
                        charge_box_serial_number=kwargs.get("charge_box_serial_number"),
                        firmware_version=kwargs.get("firmware_version"),
                        iccid=kwargs.get("iccid"),
                        imsi=kwargs.get("imsi"),
                        meter_type=kwargs.get("meter_type"),
                        meter_serial_number=kwargs.get("meter_serial_number"),
                        is_online=True,
                        last_seen=utc_now_naive(),
                        boot_status="Accepted",
                    )
                    session.add(charge_point)
                    logger.info(f"Created new charge point: {self.id}")
                else:
                    # Update existing charge point
                    charge_point.vendor = charge_point_vendor
                    charge_point.model = charge_point_model
                    charge_point.charge_point_serial_number = kwargs.get(
                        "charge_point_serial_number"
                    )
                    charge_point.charge_box_serial_number = kwargs.get(
                        "charge_box_serial_number"
                    )
                    charge_point.firmware_version = kwargs.get("firmware_version")
                    charge_point.iccid = kwargs.get("iccid")
                    charge_point.imsi = kwargs.get("imsi")
                    charge_point.meter_type = kwargs.get("meter_type")
                    charge_point.meter_serial_number = kwargs.get("meter_serial_number")
                    charge_point.is_online = True
                    charge_point.last_seen = utc_now_naive()
                    charge_point.boot_status = "Accepted"
                    charge_point.updated_at = utc_now_naive()
                    logger.info(f"Updated existing charge point: {self.id}")

                await session.commit()
                logger.info(f"BootNotification data stored in database for {self.id}")

            except Exception as e:
                logger.error(f"Failed to store BootNotification data: {e}")
                await session.rollback()
                # Continue with response even if DB fails

        # Return OCPP 1.6 compliant response
        return call_result.BootNotification(
            current_time=utc_now_iso(),
            interval=300,  # Heartbeat interval in seconds
            status="Accepted",
        )

    @on("Heartbeat")
    async def on_heartbeat(self):
        """Handle Heartbeat requests - update last_seen timestamp."""
        logger.info(f"Heartbeat received from {self.id}")

        # Update last_seen timestamp in database
        async with AsyncSessionLocal() as session:
            try:
                charge_point = await session.get(ChargePointModel, self.id)

                if charge_point:
                    charge_point.last_seen = utc_now_naive()
                    charge_point.is_online = True
                    charge_point.updated_at = utc_now_naive()
                    await session.commit()
                    logger.debug(f"Updated last_seen for charge point {self.id}")
                else:
                    logger.warning(
                        f"Received heartbeat from unknown charge point: {self.id}"
                    )

            except Exception as e:
                logger.error(f"Failed to update heartbeat timestamp: {e}")
                await session.rollback()
                # Continue with response even if DB fails

        # Return OCPP 1.6 compliant response with current time
        return call_result.Heartbeat(current_time=utc_now_iso())

    @on("Authorize")
    async def on_authorize(self, id_tag):
        """Handle Authorize requests - validate ID tag against database."""
        logger.info(f"Authorize request received from {self.id} for idTag: {id_tag}")

        # Get ID tag info (reuse logic from authorize)
        id_tag_info = await self._get_id_tag_info(id_tag)

        # Return OCPP 1.6 compliant response
        return call_result.Authorize(id_tag_info=id_tag_info)

    @on("StartTransaction")
    async def on_start_transaction(
        self, connector_id, id_tag, meter_start, timestamp, **kwargs
    ):
        """Handle StartTransaction requests - create new charging session."""
        logger.info(
            f"StartTransaction received from {self.id}: connector={connector_id}, idTag={id_tag}, meterStart={meter_start}"
        )

        # Get ID tag info and validate authorization
        id_tag_info = await self._get_id_tag_info(id_tag)

        # Default transaction ID (will be updated if session is created)
        transaction_id = 0

        # Only create session if tag is accepted
        if id_tag_info["status"] == "Accepted":
            async with AsyncSessionLocal() as session:
                try:
                    # Get the ID tag record
                    tag_result = await session.execute(
                        select(IdTag).where(IdTag.tag == id_tag)
                    )
                    tag_record = tag_result.scalar_one_or_none()

                    if tag_record:
                        # Generate unique transaction ID
                        transaction_id = await self._generate_transaction_id(session)

                        # Parse timestamp
                        if isinstance(timestamp, str):
                            start_time = parse_ocpp_timestamp(timestamp)
                        else:
                            start_time = timestamp

                        # Create new session
                        new_session = Session(
                            transaction_id=transaction_id,
                            charge_point_id=self.id,
                            id_tag_id=tag_record.id,
                            connector_id=connector_id,
                            meter_start=meter_start,
                            start_timestamp=start_time,
                            status="Active",
                            reservation_id=kwargs.get("reservation_id"),
                        )
                        session.add(new_session)
                        await session.commit()

                        logger.info(
                            f"Created session {transaction_id} for {self.id} connector {connector_id}"
                        )
                    else:
                        logger.error(f"ID tag {id_tag} not found when creating session")
                        id_tag_info = {"status": "Invalid"}

                except Exception as e:
                    logger.error(f"Failed to create session: {e}")
                    await session.rollback()
                    id_tag_info = {"status": "Invalid"}
        else:
            logger.info(
                f"StartTransaction rejected for {id_tag}: {id_tag_info['status']}"
            )

        # Return OCPP 1.6 compliant response
        return call_result.StartTransaction(
            id_tag_info=id_tag_info, transaction_id=transaction_id
        )

    @on("MeterValues")
    async def on_meter_values(self, connector_id, meter_value, **kwargs):
        """Handle MeterValues requests - store meter readings during charging sessions."""
        transaction_id = kwargs.get("transaction_id")

        logger.info(
            f"MeterValues received from {self.id}: connector={connector_id}, "
            f"transaction_id={transaction_id}, values_count={len(meter_value)}"
        )

        # Store meter values in database
        async with AsyncSessionLocal() as session:
            try:
                # If transaction_id is provided, verify it exists and get the session
                session_record = None
                if transaction_id:
                    session_result = await session.execute(
                        select(Session).where(Session.transaction_id == transaction_id)
                    )
                    session_record = session_result.scalar_one_or_none()

                    if not session_record:
                        logger.warning(
                            f"Transaction {transaction_id} not found for MeterValues from {self.id}"
                        )

                # Process each meter value
                for meter_val in meter_value:
                    # Parse timestamp
                    timestamp = meter_val.get("timestamp")
                    if isinstance(timestamp, str):
                        meter_timestamp = parse_ocpp_timestamp(timestamp)
                    else:
                        meter_timestamp = timestamp or utc_now_naive()

                    # Process each sampled value within this meter value
                    sampled_values = meter_val.get("sampledValue", [])

                    for sampled_val in sampled_values:
                        # Extract sampled value fields
                        value = sampled_val.get("value")
                        context = sampled_val.get("context", "Sample.Periodic")
                        format_type = sampled_val.get("format", "Raw")
                        measurand = sampled_val.get(
                            "measurand", "Energy.Active.Import.Register"
                        )
                        phase = sampled_val.get("phase")
                        location = sampled_val.get("location", "Outlet")
                        unit = sampled_val.get("unit", "Wh")

                        # Convert value to float for storage
                        try:
                            numeric_value = float(value)
                        except (ValueError, TypeError):
                            logger.error(f"Invalid meter value: {value}")
                            continue

                        # Create MeterValue record
                        meter_value_record = MeterValue(
                            session_id=session_record.id if session_record else None,
                            timestamp=meter_timestamp,
                            value=numeric_value,
                            unit=unit,
                            measurand=measurand,
                            phase=phase,
                            location=location,
                            context=context,
                            format=format_type,
                        )

                        session.add(meter_value_record)

                        logger.debug(
                            f"Stored meter value: {numeric_value} {unit} "
                            f"({measurand}) at {meter_timestamp}"
                        )

                await session.commit()
                logger.info(
                    f"Successfully stored {len(meter_value)} meter value sets "
                    f"from {self.id} connector {connector_id}"
                )

            except Exception as e:
                logger.error(f"Failed to store meter values: {e}")
                await session.rollback()
                # Continue with response even if DB fails

        # Return OCPP 1.6 compliant response (empty response)
        return call_result.MeterValues()

    @on("StopTransaction")
    async def on_stop_transaction(
        self, transaction_id, timestamp, meter_stop, **kwargs
    ):
        """Handle StopTransaction requests - end charging session and store final data."""
        id_tag = kwargs.get("id_tag")
        reason = kwargs.get("reason", "Local")
        transaction_data = kwargs.get("transaction_data", [])

        logger.info(
            f"StopTransaction received from {self.id}: transaction_id={transaction_id}, "
            f"meter_stop={meter_stop}, reason={reason}, id_tag={id_tag}"
        )

        # Default response (no idTagInfo)
        response_data = {}

        # Parse timestamp
        if isinstance(timestamp, str):
            stop_time = parse_ocpp_timestamp(timestamp)
        else:
            stop_time = timestamp

        # Update session in database
        async with AsyncSessionLocal() as session:
            try:
                # Find the session by transaction_id
                session_result = await session.execute(
                    select(Session).where(Session.transaction_id == transaction_id)
                )
                session_record = session_result.scalar_one_or_none()

                if not session_record:
                    logger.error(
                        f"Transaction {transaction_id} not found for StopTransaction from {self.id}"
                    )
                    # Still return success response as per OCPP spec
                else:
                    # Update session with stop data
                    session_record.meter_stop = meter_stop
                    session_record.stop_timestamp = stop_time
                    session_record.status = "Completed"
                    session_record.stop_reason = reason
                    session_record.updated_at = utc_now_naive()

                    # Calculate energy consumed if we have both start and stop values
                    if session_record.meter_start is not None:
                        # Convert from Wh to kWh for energy_consumed field
                        energy_wh = meter_stop - session_record.meter_start
                        session_record.energy_consumed = energy_wh / 1000.0

                    logger.info(
                        f"Updated session {transaction_id}: meter_stop={meter_stop}, "
                        f"energy_consumed={session_record.energy_consumed} kWh"
                    )

                # Process transaction data (additional meter values) if provided
                if transaction_data:
                    logger.info(
                        f"Processing {len(transaction_data)} transaction data entries"
                    )

                    for meter_val in transaction_data:
                        # Parse timestamp
                        data_timestamp = meter_val.get("timestamp")
                        if isinstance(data_timestamp, str):
                            meter_timestamp = parse_ocpp_timestamp(data_timestamp)
                        else:
                            meter_timestamp = data_timestamp or utc_now_naive()

                        # Process each sampled value
                        sampled_values = meter_val.get("sampledValue", [])

                        for sampled_val in sampled_values:
                            # Extract sampled value fields
                            value = sampled_val.get("value")
                            context = sampled_val.get("context", "Transaction.End")
                            format_type = sampled_val.get("format", "Raw")
                            measurand = sampled_val.get(
                                "measurand", "Energy.Active.Import.Register"
                            )
                            phase = sampled_val.get("phase")
                            location = sampled_val.get("location", "Outlet")
                            unit = sampled_val.get("unit", "Wh")

                            # Convert value to float for storage
                            try:
                                numeric_value = float(value)
                            except (ValueError, TypeError):
                                logger.error(f"Invalid transaction data value: {value}")
                                continue

                            # Create MeterValue record
                            meter_value_record = MeterValue(
                                session_id=(
                                    session_record.id if session_record else None
                                ),
                                timestamp=meter_timestamp,
                                value=numeric_value,
                                unit=unit,
                                measurand=measurand,
                                phase=phase,
                                location=location,
                                context=context,
                                format=format_type,
                            )

                            session.add(meter_value_record)

                            logger.debug(
                                f"Stored transaction data: {numeric_value} {unit} "
                                f"({measurand}) at {meter_timestamp}"
                            )

                # Validate idTag if provided and add to response
                if id_tag:
                    id_tag_info = await self._get_id_tag_info(id_tag)
                    response_data["id_tag_info"] = id_tag_info
                    logger.info(
                        f"ID tag {id_tag} validation for stop: {id_tag_info['status']}"
                    )

                await session.commit()
                logger.info(
                    f"Successfully processed StopTransaction for transaction {transaction_id}"
                )

            except Exception as e:
                logger.error(f"Failed to process StopTransaction: {e}")
                await session.rollback()
                # Continue with response even if DB fails - OCPP spec requirement

        # Return OCPP 1.6 compliant response
        return call_result.StopTransaction(**response_data)

    @on("StatusNotification")
    async def on_status_notification(self, connector_id, error_code, status, **kwargs):
        """Handle StatusNotification requests - track connector status changes."""
        timestamp = kwargs.get("timestamp")
        info = kwargs.get("info")
        vendor_id = kwargs.get("vendor_id")
        vendor_error_code = kwargs.get("vendor_error_code")

        logger.info(
            f"StatusNotification received from {self.id}: connector_id={connector_id}, "
            f"status={status}, error_code={error_code}, info={info}"
        )

        # Parse timestamp if provided
        status_timestamp = None
        if timestamp:
            if isinstance(timestamp, str):
                status_timestamp = parse_ocpp_timestamp(timestamp)
            else:
                # If it's already a datetime object, ensure it's timezone-naive UTC
                if hasattr(timestamp, "tzinfo") and timestamp.tzinfo is not None:
                    status_timestamp = timestamp.astimezone(timezone.utc).replace(
                        tzinfo=None
                    )
                else:
                    status_timestamp = timestamp
        else:
            # Use current UTC time as timezone-naive
            status_timestamp = utc_now_naive()

        # Store status notification in database
        async with AsyncSessionLocal() as session:
            try:
                # Verify charge point exists
                charge_point = await session.get(ChargePointModel, self.id)
                if not charge_point:
                    logger.warning(
                        f"Received StatusNotification from unknown charge point: {self.id}"
                    )
                    # Still process the notification for logging purposes

                # Create status notification record
                status_record = ConnectorStatus(
                    charge_point_id=self.id,
                    connector_id=connector_id,
                    status=status,
                    error_code=error_code,
                    timestamp=status_timestamp,
                    info=info,
                    vendor_id=vendor_id,
                    vendor_error_code=vendor_error_code,
                )

                session.add(status_record)

                # Update charge point's overall status if this is connector 0
                if connector_id == 0 and charge_point:
                    charge_point.status = status
                    charge_point.updated_at = utc_now_naive()
                    logger.info(
                        f"Updated charge point {self.id} overall status to: {status}"
                    )

                await session.commit()
                logger.info(
                    f"Stored StatusNotification for {self.id} connector {connector_id}: "
                    f"{status} ({error_code})"
                )

                # Log important status changes
                if error_code != "NoError":
                    logger.warning(
                        f"Connector {connector_id} on {self.id} reported error: "
                        f"{error_code} - {info or 'No additional info'}"
                    )

                # Log status transitions that indicate charging activity
                if status in [
                    "Preparing",
                    "Charging",
                    "SuspendedEV",
                    "SuspendedEVSE",
                    "Finishing",
                ]:
                    logger.info(
                        f"Connector {connector_id} on {self.id} is now {status} "
                        f"({info or 'No additional info'})"
                    )

            except Exception as e:
                logger.error(f"Failed to store StatusNotification: {e}")
                await session.rollback()
                # Continue with response even if DB fails

        # Return OCPP 1.6 compliant response (empty response)
        return call_result.StatusNotification()

    @on("RemoteStartTransaction")
    async def on_remote_start_transaction(self, id_tag, **kwargs):
        """Handle RemoteStartTransaction requests from Central System."""
        connector_id = kwargs.get("connector_id")
        charging_profile = kwargs.get("charging_profile")

        logger.info(
            f"RemoteStartTransaction received from Central System for {self.id}: "
            f"idTag={id_tag}, connectorId={connector_id}"
        )

        # Default response status
        status = "Rejected"

        async with AsyncSessionLocal() as session:
            try:
                # Validate the ID tag first
                id_tag_info = await self._get_id_tag_info(id_tag)

                if id_tag_info["status"] != "Accepted":
                    logger.info(
                        f"RemoteStartTransaction rejected for {id_tag}: {id_tag_info['status']}"
                    )
                    status = "Rejected"
                else:
                    # Check charge point status
                    charge_point = await session.get(ChargePointModel, self.id)
                    if not charge_point or not charge_point.is_online:
                        logger.warning(
                            f"RemoteStartTransaction rejected: charge point {self.id} is offline"
                        )
                        status = "Rejected"
                    else:
                        # If connector_id is specified, check if it's available
                        if connector_id is not None:
                            # Check connector status
                            connector_status_result = await session.execute(
                                select(ConnectorStatus)
                                .where(ConnectorStatus.charge_point_id == self.id)
                                .where(ConnectorStatus.connector_id == connector_id)
                                .order_by(ConnectorStatus.timestamp.desc())
                                .limit(1)
                            )
                            latest_status = connector_status_result.scalar_one_or_none()

                            if latest_status and latest_status.status not in [
                                "Available",
                                "Preparing",
                            ]:
                                logger.info(
                                    f"RemoteStartTransaction rejected: connector {connector_id} "
                                    f"is {latest_status.status}"
                                )
                                status = "Rejected"
                            else:
                                status = "Accepted"
                                logger.info(
                                    f"RemoteStartTransaction accepted for connector {connector_id}"
                                )
                        else:
                            # No specific connector requested - find an available one
                            # For simplicity, we'll accept if charge point is online
                            status = "Accepted"
                            logger.info(
                                "RemoteStartTransaction accepted (no specific connector)"
                            )

                        # If accepted, we would typically trigger the charge point to:
                        # 1. Send StatusNotification (Preparing)
                        # 2. Automatically start a transaction
                        # 3. Send StartTransaction request
                        # For now, we'll just log this and let the charge point handle it

                        if status == "Accepted":
                            logger.info(
                                f"Charge point {self.id} should now prepare for transaction "
                                f"with idTag {id_tag}"
                            )

                            # Log charging profile if provided
                            if charging_profile:
                                logger.info(
                                    f"Charging profile provided: ID={charging_profile.get('chargingProfileId')}, "
                                    f"purpose={charging_profile.get('chargingProfilePurpose')}"
                                )

            except Exception as e:
                logger.error(f"Failed to process RemoteStartTransaction: {e}")
                await session.rollback()
                status = "Rejected"

        # Return OCPP 1.6 compliant response
        return call_result.RemoteStartTransaction(status=status)

    @on("RemoteStopTransaction")
    async def on_remote_stop_transaction(self, transaction_id):
        """
        Handle RemoteStopTransaction requests from Central System.

        According to OCPP 1.6 Section 5.12:
        - Validate that transaction exists and is active
        - Return Accepted if transaction can be stopped
        - Return Rejected if transaction doesn't exist or cannot be stopped
        - If Accepted, automatically trigger StopTransaction flow
        """
        logger.info(
            f"RemoteStopTransaction received from Central System for {self.id}: "
            f"transactionId={transaction_id}"
        )

        # Default response status
        status = "Rejected"

        async with AsyncSessionLocal() as session:
            try:
                # Validate that the transaction exists and is active
                session_result = await session.execute(
                    select(Session)
                    .where(Session.transaction_id == transaction_id)
                    .where(Session.charge_point_id == self.id)
                )
                session_record = session_result.scalar_one_or_none()

                if not session_record:
                    logger.info(
                        f"RemoteStopTransaction rejected: transaction {transaction_id} "
                        f"not found for charge point {self.id}"
                    )
                    status = "Rejected"
                elif session_record.status != "Active":
                    logger.info(
                        f"RemoteStopTransaction rejected: transaction {transaction_id} "
                        f"is not active (status: {session_record.status})"
                    )
                    status = "Rejected"
                else:
                    # Transaction exists and is active - accept the request
                    status = "Accepted"
                    logger.info(
                        f"RemoteStopTransaction accepted for transaction {transaction_id} "
                        f"on connector {session_record.connector_id}"
                    )

                    # According to OCPP spec: "This remote request to stop a transaction
                    # is equal to a local action to stop a transaction. Therefore, the
                    # transaction SHALL be stopped"

                    # Schedule automatic StopTransaction to be sent after response
                    # This simulates the charge point automatically stopping the transaction
                    asyncio.create_task(
                        self._send_automatic_stop_transaction(
                            transaction_id,
                            session_record.connector_id,
                            session_record.meter_start or 0,
                        )
                    )

            except Exception as e:
                logger.error(f"Failed to process RemoteStopTransaction: {e}")
                await session.rollback()
                status = "Rejected"

        # Return OCPP 1.6 compliant response
        return call_result.RemoteStopTransaction(status=status)

    @on("ChangeAvailability")
    async def on_change_availability(self, connector_id, type):
        """
        Handle ChangeAvailability requests from Central System.

        According to OCPP 1.6 Section 5.2:
        - Central System can request Charge Point to change availability
        - Response: "Accepted", "Rejected", or "Scheduled"
        - "Scheduled" when transaction is in progress
        - "Accepted" for immediate change or already in requested state
        - ConnectorId = 0 applies to Charge Point and all connectors
        - Must persist across reboots and send StatusNotification after change

        MVP: Single ChargePoint with single Connector (connector_id = 1)
        """
        logger.info(
            f"ChangeAvailability received from Central System for {self.id}: "
            f"connectorId={connector_id}, type={type}"
        )

        # Default response status
        status = "Rejected"

        async with AsyncSessionLocal() as session:
            try:
                # MVP: Validate connector_id (0 = ChargePoint, 1 = single connector)
                if connector_id < 0 or connector_id > 1:
                    logger.info(
                        f"ChangeAvailability rejected: invalid connector_id {connector_id} "
                        f"(MVP supports 0=ChargePoint, 1=connector)"
                    )
                    status = "Rejected"
                else:
                    # Check for active transactions if affecting connector 1
                    # (connector_id = 0 affects all connectors, including 1)
                    has_active_transaction = False

                    if connector_id == 0 or connector_id == 1:
                        # Check if connector 1 has active transaction
                        active_session_result = await session.execute(
                            select(Session)
                            .where(Session.charge_point_id == self.id)
                            .where(Session.connector_id == 1)
                            .where(Session.status == "Active")
                        )
                        active_session = active_session_result.scalar_one_or_none()
                        has_active_transaction = active_session is not None

                    if has_active_transaction:
                        # OCPP: "When a transaction is in progress Charge Point SHALL respond
                        # with availability status 'Scheduled'"
                        status = "Scheduled"
                        logger.info(
                            f"ChangeAvailability scheduled: transaction in progress on connector 1"
                        )
                    else:
                        # Get current availability state(s)
                        if connector_id == 0:
                            # ConnectorId = 0: Apply to ChargePoint and all connectors
                            target_connectors = [0, 1]
                        else:
                            # ConnectorId = 1: Apply to specific connector only
                            target_connectors = [1]

                        # Check if already in requested state
                        already_in_state = True
                        for target_connector_id in target_connectors:
                            current_status_result = await session.execute(
                                select(ConnectorStatus)
                                .where(ConnectorStatus.charge_point_id == self.id)
                                .where(
                                    ConnectorStatus.connector_id == target_connector_id
                                )
                                .order_by(ConnectorStatus.created_at.desc())
                                .limit(1)
                            )
                            current_status = current_status_result.scalar_one_or_none()

                            if (
                                not current_status
                                or current_status.availability != type
                            ):
                                already_in_state = False
                                break

                        if already_in_state:
                            # OCPP: "In the event that Central System requests Charge Point to change
                            # to a status it is already in, Charge Point SHALL respond with
                            # availability status 'Accepted'"
                            status = "Accepted"
                            logger.info(
                                f"ChangeAvailability accepted: already in {type} state"
                            )
                        else:
                            # Apply availability change immediately
                            for target_connector_id in target_connectors:
                                # Update the latest ConnectorStatus record for this connector
                                latest_status_result = await session.execute(
                                    select(ConnectorStatus)
                                    .where(ConnectorStatus.charge_point_id == self.id)
                                    .where(
                                        ConnectorStatus.connector_id
                                        == target_connector_id
                                    )
                                    .order_by(ConnectorStatus.created_at.desc())
                                    .limit(1)
                                )
                                latest_status = (
                                    latest_status_result.scalar_one_or_none()
                                )

                                if latest_status:
                                    # Update existing record
                                    latest_status.availability = type
                                else:
                                    # Create new record if none exists
                                    new_status = ConnectorStatus(
                                        charge_point_id=self.id,
                                        connector_id=target_connector_id,
                                        status=(
                                            "Available"
                                            if type == "Operative"
                                            else "Unavailable"
                                        ),
                                        error_code="NoError",
                                        availability=type,
                                        timestamp=utc_now_naive(),
                                    )
                                    session.add(new_status)

                            await session.commit()
                            status = "Accepted"
                            logger.info(
                                f"ChangeAvailability accepted: updated connector(s) {target_connectors} "
                                f"to {type}"
                            )

                            # Schedule StatusNotification to be sent after response
                            # OCPP: "When an availability change requested with a ChangeAvailability.req
                            # PDU has happened, the Charge Point SHALL inform Central System of its new
                            # availability status with a StatusNotification.req"
                            asyncio.create_task(
                                self._send_availability_status_notifications(
                                    target_connectors, type
                                )
                            )

            except Exception as e:
                logger.error(f"Failed to process ChangeAvailability: {e}")
                await session.rollback()
                status = "Rejected"

        # Return OCPP 1.6 compliant response
        return call_result.ChangeAvailability(status=status)

    async def _send_automatic_stop_transaction(
        self, transaction_id: int, connector_id: int, meter_start: int
    ):
        """
        Automatically send StopTransaction after RemoteStopTransaction is accepted.

        This simulates the charge point stopping the transaction as required by OCPP spec.
        """
        try:
            # Wait a moment to ensure the RemoteStopTransaction response is sent first
            await asyncio.sleep(1.0)

            # Calculate a realistic meter stop value (simulate some energy consumption)
            # In a real charge point, this would come from the actual meter
            meter_stop = meter_start + random.randint(1000, 5000)  # Add 1-5 kWh

            logger.info(
                f"Automatically stopping transaction {transaction_id} on connector {connector_id} "
                f"(meter: {meter_start} -> {meter_stop} Wh)"
            )

            # Send StopTransaction request as required by OCPP spec
            request = call.StopTransaction(
                transaction_id=transaction_id,
                meter_stop=meter_stop,
                timestamp=utc_now_iso(),
                reason="Remote",  # Indicate this was a remote stop
            )

            response = await self.call(request)
            logger.info(
                f"Automatic StopTransaction sent for transaction {transaction_id}: "
                f"response received"
            )

            # Send StatusNotification to indicate connector is now available
            # This simulates the connector becoming available after transaction stops
            await asyncio.sleep(0.5)  # Small delay between messages

            status_request = call.StatusNotification(
                connector_id=connector_id,
                error_code="NoError",
                status="Available",
                info="Transaction stopped remotely",
                timestamp=utc_now_iso(),
            )

            await self.call(status_request)
            logger.info(
                f"StatusNotification sent: connector {connector_id} now Available "
                f"after remote stop"
            )

        except Exception as e:
            logger.error(
                f"Failed to send automatic StopTransaction for {transaction_id}: {e}"
            )

    async def _get_id_tag_info(self, id_tag):
        """Helper method to get ID tag info (shared between Authorize and StartTransaction)."""
        # Default response for unknown/invalid tags
        id_tag_info = {"status": "Invalid"}

        # Look up ID tag in database
        async with AsyncSessionLocal() as session:
            try:
                # Query for the ID tag
                result = await session.execute(select(IdTag).where(IdTag.tag == id_tag))
                tag_record = result.scalar_one_or_none()

                if tag_record:
                    # Check if tag is expired
                    if (
                        tag_record.expiry_date
                        and tag_record.expiry_date < utc_now_naive()
                    ):
                        id_tag_info = {
                            "status": "Expired",
                            "expiryDate": tag_record.expiry_date.isoformat(),
                        }
                        logger.info(f"ID tag {id_tag} is expired")
                    else:
                        # Use the tag's current status
                        id_tag_info = {"status": tag_record.status}

                        # Add optional fields if present
                        if tag_record.expiry_date:
                            id_tag_info["expiryDate"] = (
                                tag_record.expiry_date.isoformat()
                            )
                        if tag_record.parent_id_tag:
                            id_tag_info["parentIdTag"] = tag_record.parent_id_tag

                        logger.info(
                            f"ID tag {id_tag} authorized with status: {tag_record.status}"
                        )
                else:
                    logger.info(f"ID tag {id_tag} not found in database")

            except Exception as e:
                logger.error(f"Failed to lookup ID tag {id_tag}: {e}")
                # Return Invalid status on database error
                id_tag_info = {"status": "Invalid"}

        return id_tag_info

    async def _generate_transaction_id(self, session):
        """Generate a unique transaction ID."""
        # Simple approach: use random number and check for uniqueness
        # In production, you might use a sequence or UUID
        while True:
            transaction_id = random.randint(100000, 999999)

            # Check if this ID already exists
            result = await session.execute(
                select(Session).where(Session.transaction_id == transaction_id)
            )
            existing = result.scalar_one_or_none()

            if not existing:
                return transaction_id

    async def _send_availability_status_notifications(
        self, connector_ids, availability_type
    ):
        """
        Send StatusNotification messages after availability changes.

        According to OCPP 1.6 Section 5.2:
        "When an availability change requested with a ChangeAvailability.req PDU has happened,
        the Charge Point SHALL inform Central System of its new availability status with a
        StatusNotification.req"

        Args:
            connector_ids: List of connector IDs that changed availability
            availability_type: "Operative" or "Inoperative"
        """
        try:
            # Wait a moment to ensure ChangeAvailability response is sent first
            await asyncio.sleep(1.0)

            for connector_id in connector_ids:
                # Determine status based on availability
                if availability_type == "Operative":
                    # Operative connectors are typically "Available" unless charging
                    status = "Available"
                    info = "Connector set to operative"
                else:
                    # Inoperative connectors are "Unavailable"
                    status = "Unavailable"
                    info = "Connector set to inoperative"

                logger.info(
                    f"Sending StatusNotification for connector {connector_id}: "
                    f"status={status}, availability={availability_type}"
                )

                # Send StatusNotification to inform Central System
                status_request = call.StatusNotification(
                    connector_id=connector_id,
                    error_code="NoError",
                    status=status,
                    info=info,
                    timestamp=utc_now_iso(),
                )

                await self.call(status_request)
                logger.info(
                    f"StatusNotification sent: connector {connector_id} now {status} "
                    f"(availability: {availability_type})"
                )

                # Small delay between notifications
                if len(connector_ids) > 1:
                    await asyncio.sleep(0.2)

        except Exception as e:
            logger.error(f"Failed to send availability StatusNotifications: {e}")
