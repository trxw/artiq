from types import SimpleNamespace

from migen import *
from migen.genlib.fsm import *
from migen.genlib.fifo import AsyncFIFO
from migen.genlib.cdc import PulseSynchronizer, NoRetiming


def layout_len(l):
    return sum(e[1] for e in l)


class PacketLayoutManager:
    def __init__(self, alignment):
        self.alignment = alignment
        self.layouts = dict()
        self.types = dict()
        self.type_names = dict()
    
    def add_type(self, name, *fields, pad=True):
        type_n = len(self.types)
        self.types[name] = type_n
        self.type_names[type_n] = name
        layout = [("ty", 8)] + list(fields)
        misalignment = layout_len(layout) % self.alignment
        if misalignment:
            layout.append(("packet_pad", self.alignment - misalignment))
        self.layouts[name] = layout


def get_m2s_layouts(alignment):
    plm = PacketLayoutManager(alignment)
    plm.add_type("echo_request")
    plm.add_type("set_time", ("timestamp", 64))
    plm.add_type("write", ("timestamp", 64),
                          ("channel", 16),
                          ("address", 16),
                          ("data_len", 8),
                          ("short_data", 8))
    plm.add_type("fifo_space_request", ("channel", 16))
    return plm


def get_s2m_layouts(alignment):
    plm = PacketLayoutManager(alignment)
    plm.add_type("error", ("code", 8))
    plm.add_type("echo_reply")
    plm.add_type("fifo_space_reply", ("space", 16))
    return plm


error_codes = {
    "unknown_type": 0,
    # The transmitter is normally responsible for avoiding
    # overflows and underflows. Those error reports are only
    # for diagnosing internal ARTIQ bugs.
    "write_overflow": 1,
    "write_underflow": 2
}


class ReceiveDatapath(Module):
    def __init__(self, frame, data, plm):
        ws = len(data)

        # control
        self.packet_buffer_load = Signal()

        # outputs
        self.frame_r = Signal()
        self.data_r = Signal(ws)
        self.packet_type = Signal(8)
        self.packet_last = Signal()
        self.packet_as = dict()

        # # #

        # input pipeline stage - determine packet length based on type
        lastword_per_type = [layout_len(plm.layouts[plm.type_names[i]])//ws - 1
                             for i in range(len(plm.layouts))]
        packet_last_n = Signal(max=max(lastword_per_type)+1)
        self.sync += [
            self.frame_r.eq(frame),
            self.data_r.eq(data),
            If(frame & ~self.frame_r,
                self.packet_type.eq(data[:8]),
                packet_last_n.eq(Array(lastword_per_type)[data[:8]])
            )
        ]

        # bufferize packet
        packet_buffer = Signal(max(layout_len(l)
                                   for l in plm.layouts.values()))
        w_in_packet = len(packet_buffer)//ws
        packet_buffer_count = Signal(max=w_in_packet+1)
        self.sync += \
            If(self.packet_buffer_load,
                Case(packet_buffer_count, 
                     {i: packet_buffer[i*ws:(i+1)*ws].eq(self.data_r)
                      for i in range(w_in_packet)}),
                packet_buffer_count.eq(packet_buffer_count + 1)
            ).Else(
                packet_buffer_count.eq(0)
            )
        self.comb += self.packet_last.eq(packet_buffer_count == packet_last_n)

        # dissect packet
        for name, layout in plm.layouts.items():
            fields = SimpleNamespace()
            idx = 0
            for field_name, field_size in layout:
                setattr(fields, field_name, packet_buffer[idx:idx+field_size])
                idx += field_size
            self.packet_as[name] = fields


class TransmitDatapath(Module):
    def __init__(self, frame, data, plm):
        ws = len(data)
        assert ws % 8 == 0
        self.ws = ws
        self.plm = plm

        # inputs
        self.packet_buffer = Signal(max(layout_len(l)
                                        for l in plm.layouts.values()))
        w_in_packet = len(self.packet_buffer)//ws
        self.packet_len = Signal(max=w_in_packet+1)

        # control
        self.stb = Signal()
        self.done = Signal()

        # # #

        packet_buffer_count = Signal(max=w_in_packet+1)
        self.sync += [
            self.done.eq(0),
            frame.eq(0),
            packet_buffer_count.eq(0),

            If(self.stb & ~self.done,
                If(packet_buffer_count == self.packet_len,
                    self.done.eq(1)
                ).Else(
                    frame.eq(1),
                    Case(packet_buffer_count, 
                         {i: data.eq(self.packet_buffer[i*ws:(i+1)*ws])
                          for i in range(w_in_packet)}),
                    packet_buffer_count.eq(packet_buffer_count + 1)
                )
            )
        ]

    def send(self, ty, **kwargs):
        idx = 8
        value = self.plm.types[ty]
        for field_name, field_size in self.plm.layouts[ty][1:]:
            try:
                fvalue = kwargs[field_name]
                del kwargs[field_name]
            except KeyError:
                fvalue = 0
            value = value | (fvalue << idx)
            idx += field_size
        if kwargs:
            raise ValueError
        return [
            self.packet_buffer.eq(value),
            self.packet_len.eq(idx//self.ws)
        ]


class RTPacketSatellite(Module):
    def __init__(self, link_layer):
        self.tsc_load = Signal()
        self.tsc_value = Signal(64)
        
        self.fifo_space_channel = Signal(16)
        self.fifo_space_update = Signal()
        self.fifo_space = Signal(16)
        
        self.write_stb = Signal()
        self.write_timestamp = Signal(64)
        self.write_channel = Signal(16)
        self.write_address = Signal(16)
        self.write_data = Signal(256)
        self.write_overflow = Signal()
        self.write_overflow_ack = Signal()
        self.write_underflow = Signal()
        self.write_underflow_ack = Signal()

        # # #

        # RX/TX datapath
        assert len(link_layer.tx_rt_data) == len(link_layer.rx_rt_data)
        assert len(link_layer.tx_rt_data) % 8 == 0
        ws = len(link_layer.tx_rt_data)
        rx_plm = get_m2s_layouts(ws)
        rx_dp = ReceiveDatapath(
            link_layer.rx_rt_frame, link_layer.rx_rt_data, rx_plm)
        self.submodules += rx_dp
        tx_plm = get_s2m_layouts(ws)
        tx_dp = TransmitDatapath(
            link_layer.tx_rt_frame, link_layer.tx_rt_data, tx_plm)
        self.submodules += tx_dp

        # RX->TX
        echo_req = Signal()
        err_set = Signal()
        err_req = Signal()
        err_ack = Signal()
        fifo_space_set = Signal()
        fifo_space_req = Signal()
        fifo_space_ack = Signal()
        self.sync += [
            If(err_ack, err_req.eq(0)),
            If(err_set, err_req.eq(1)),
            If(fifo_space_ack, fifo_space_req.eq(0)),
            If(fifo_space_set, fifo_space_req.eq(1)),
        ]
        err_code = Signal(max=len(error_codes)+1)

        # RX FSM
        self.comb += [
            self.tsc_value.eq(
                rx_dp.packet_as["set_time"].timestamp),
            self.fifo_space_channel.eq(
                rx_dp.packet_as["fifo_space_request"].channel),
            self.write_timestamp.eq(
                rx_dp.packet_as["write"].timestamp),
            self.write_channel.eq(
                rx_dp.packet_as["write"].channel),
            self.write_address.eq(
                rx_dp.packet_as["write"].address),
            self.write_data.eq(
                rx_dp.packet_as["write"].short_data)
        ]

        rx_fsm = FSM(reset_state="INPUT")
        self.submodules += rx_fsm

        rx_fsm.act("INPUT",
            If(rx_dp.frame_r,
                rx_dp.packet_buffer_load.eq(1),
                If(rx_dp.packet_last,
                    Case(rx_dp.packet_type, {
                        # echo must have fixed latency, so there is no memory
                        # mechanism
                        rx_plm.types["echo_request"]: echo_req.eq(1),
                        rx_plm.types["set_time"]: NextState("SET_TIME"),
                        rx_plm.types["write"]: NextState("WRITE"),
                        rx_plm.types["fifo_space_request"]:
                            NextState("FIFO_SPACE"),
                        "default": [
                            err_set.eq(1),
                            NextValue(err_code, error_codes["unknown_type"])]
                    })
                )
            )
        )
        rx_fsm.act("SET_TIME",
            self.tsc_load.eq(1),
            NextState("INPUT")
        )
        rx_fsm.act("WRITE",
            self.write_stb.eq(1),
            NextState("INPUT")
        )
        rx_fsm.act("FIFO_SPACE",
            fifo_space_set.eq(1),
            self.fifo_space_update.eq(1),
            NextState("INPUT")
        )

        # TX FSM
        tx_fsm = FSM(reset_state="IDLE")
        self.submodules += tx_fsm

        tx_fsm.act("IDLE",
            If(echo_req, NextState("ECHO")),
            If(fifo_space_req, NextState("FIFO_SPACE")),
            If(self.write_overflow, NextState("ERROR_WRITE_OVERFLOW")),
            If(self.write_underflow, NextState("ERROR_WRITE_UNDERFLOW")),
            If(err_req, NextState("ERROR"))
        )
        tx_fsm.act("ECHO",
            tx_dp.send("echo_reply"),
            tx_dp.stb.eq(1),
            If(tx_dp.done, NextState("IDLE"))
        )
        tx_fsm.act("FIFO_SPACE",
            fifo_space_ack.eq(1),
            tx_dp.send("fifo_space_reply", space=self.fifo_space),
            tx_dp.stb.eq(1),
            If(tx_dp.done, NextState("IDLE"))
        )
        tx_fsm.act("ERROR_WRITE_OVERFLOW",
            self.write_overflow_ack.eq(1),
            tx_dp.send("error", code=error_codes["write_overflow"]),
            tx_dp.stb.eq(1),
            If(tx_dp.done, NextState("IDLE"))
        )
        tx_fsm.act("ERROR_WRITE_UNDERFLOW",
            self.write_underflow_ack.eq(1),
            tx_dp.send("error", code=error_codes["write_underflow"]),
            tx_dp.stb.eq(1),
            If(tx_dp.done, NextState("IDLE"))
        )
        tx_fsm.act("ERROR",
            err_ack.eq(1),
            tx_dp.send("error", code=err_code),
            tx_dp.stb.eq(1),
            If(tx_dp.done, NextState("IDLE"))
        )


class _CrossDomainRequest(Module):
    def __init__(self, domain,
                 req_stb, req_ack, req_data,
                 srv_stb, srv_ack, srv_data):
        dsync = getattr(self.sync, domain)

        request = PulseSynchronizer("sys", domain)
        reply = PulseSynchronizer(domain, "sys")
        self.submodules += request, reply

        ongoing = Signal()
        self.comb += request.i.eq(~ongoing & req_stb)
        self.sync += [
            req_ack.eq(reply.o),
            If(req_stb, ongoing.eq(1)),
            If(req_ack, ongoing.eq(0))
        ]
        if req_data is not None:
            req_data_r = Signal.like(req_data)
            self.specials += NoRetiming(req_data_r)
            self.sync += If(req_stb, req_data_r.eq(req_data))
        dsync += [
            If(request.o, srv_stb.eq(1)),
            If(srv_ack, srv_stb.eq(0))
        ]
        if req_data is not None:
            dsync += If(request.o, srv_data.eq(req_data_r))
        self.comb += reply.i.eq(srv_stb & srv_ack)


class _CrossDomainNotification(Module):
    def __init__(self, domain,
                 emi_stb, emi_data,
                 rec_stb, rec_ack, rec_data):
        emi_data_r = Signal.like(emi_data)
        self.specials += NoRetiming(emi_data_r)
        dsync = getattr(self.sync, domain)
        dsync += If(emi_stb, emi_data_r.eq(emi_data))

        ps = PulseSynchronizer(domain, "sys")
        self.submodules += ps
        self.comb += ps.i.eq(emi_stb)
        self.sync += [
            If(rec_ack, rec_stb.eq(0)),
            If(ps.o,
                rec_data.eq(emi_data_r),
                rec_stb.eq(1)
            )
        ]


class RTPacketMaster(Module):
    def __init__(self, link_layer, write_fifo_depth=4):
        # all interface signals in sys domain unless otherwise specified

        # write interface, optimized for throughput
        self.write_stb = Signal()
        self.write_ack = Signal()
        self.write_timestamp = Signal(64)
        self.write_channel = Signal(16)
        self.write_address = Signal(16)
        self.write_data = Signal(256)

        # fifo space interface
        # write with timestamp[48:] == 0xffff to make a fifo space request
        # (space requests have to be ordered wrt writes)
        self.fifo_space_not = Signal()
        self.fifo_space_not_ack = Signal()
        self.fifo_space = Signal(16)

        # echo interface
        self.echo_stb = Signal()
        self.echo_ack = Signal()
        self.echo_sent_now = Signal()  # in rtio domain
        self.echo_received_now = Signal()  # in rtio_rx domain

        # set_time interface
        self.set_time_stb = Signal()
        self.set_time_ack = Signal()
        # in rtio domain, must be valid all time while there is
        # a set_time request pending
        self.tsc_value = Signal(64)

        # errors
        self.error_not = Signal()
        self.error_not_ack = Signal()
        self.error_code = Signal(8)

        # # #

        # CDC
        wfifo = ClockDomainsRenamer({"write": "sys", "read": "rtio"})(
            AsyncFIFO(64+16+16+256, write_fifo_depth))
        self.submodules += wfifo
        write_timestamp = Signal(64)
        write_channel = Signal(16)
        write_address = Signal(16)
        write_data = Signal(256)
        self.comb += [
            wfifo.we.eq(self.write_stb),
            self.write_ack.eq(wfifo.writable),
            wfifo.din.eq(Cat(self.write_timestamp, self.write_channel,
                             self.write_address, self.write_data)),
            Cat(write_timestamp, write_channel,
                write_address, write_data).eq(wfifo.dout)
        ]

        fifo_space_not = Signal()
        fifo_space = Signal(16)
        self.submodules += _CrossDomainNotification("rtio_rx",
            fifo_space_not, fifo_space,
            self.fifo_space_not, self.fifo_space_not_ack, self.fifo_space)

        set_time_stb = Signal()
        set_time_ack = Signal()
        self.submodules += _CrossDomainRequest("rtio",
            self.set_time_stb, self.set_time_ack, None,
            set_time_stb, set_time_ack, None)

        echo_stb = Signal()
        echo_ack = Signal()
        self.submodules += _CrossDomainRequest("rtio",
            self.echo_stb, self.echo_ack, None,
            echo_stb, echo_ack, None)

        error_not = Signal()
        error_code = Signal(8)
        self.submodules += _CrossDomainNotification("rtio_rx",
            error_not, error_code,
            self.error_not, self.error_not_ack, self.error_code)

        # RX/TX datapath
        assert len(link_layer.tx_rt_data) == len(link_layer.rx_rt_data)
        assert len(link_layer.tx_rt_data) % 8 == 0
        ws = len(link_layer.tx_rt_data)
        tx_plm = get_m2s_layouts(ws)
        tx_dp = ClockDomainsRenamer("rtio")(TransmitDatapath(
            link_layer.tx_rt_frame, link_layer.tx_rt_data, tx_plm))
        self.submodules += tx_dp
        rx_plm = get_s2m_layouts(ws)
        rx_dp = ClockDomainsRenamer("rtio_rx")(ReceiveDatapath(
            link_layer.rx_rt_frame, link_layer.rx_rt_data, rx_plm))
        self.submodules += rx_dp

        # TX FSM
        tx_fsm = ClockDomainsRenamer("rtio")(FSM(reset_state="IDLE_WRITE"))
        self.submodules += tx_fsm

        echo_sent_now = Signal()
        self.sync.rtio += self.echo_sent_now.eq(echo_sent_now)
        tsc_value = Signal(64)
        tsc_value_load = Signal()
        self.sync.rtio += If(tsc_value_load, tsc_value.eq(self.tsc_value))

        tx_fsm.act("IDLE_WRITE",
            tx_dp.send("write",
                timestamp=write_timestamp,
                channel=write_channel,
                address=write_address,
                short_data=write_data),
            If(wfifo.readable,
                If(write_timestamp[48:] == 0xffff,
                    NextState("FIFO_SPACE")
                ).Else(
                    tx_dp.stb.eq(1),
                    wfifo.re.eq(tx_dp.done)
                )
            ).Else(
                If(echo_stb,
                    echo_sent_now.eq(1),
                    NextState("ECHO")
                ).Elif(set_time_stb,
                    tsc_value_load.eq(1),
                    NextState("SET_TIME")
                )
            )
        )
        tx_fsm.act("FIFO_SPACE",
            tx_dp.send("fifo_space_request", channel=write_channel),
            tx_dp.stb.eq(1),
            If(tx_dp.done,
                wfifo.re.eq(1),
                NextState("IDLE_WRITE")
            )
        )
        tx_fsm.act("ECHO",
            tx_dp.send("echo_request"),
            tx_dp.stb.eq(1),
            If(tx_dp.done, NextState("IDLE_WRITE"))
        )
        tx_fsm.act("SET_TIME",
            tx_dp.send("set_time", timestamp=tsc_value),
            tx_dp.stb.eq(1),
            If(tx_dp.done, NextState("IDLE_WRITE"))
        )

        # RX FSM
        rx_fsm = ClockDomainsRenamer("rtio_rx")(FSM(reset_state="INPUT"))
        self.submodules += rx_fsm

        echo_received_now = Signal()
        self.sync.rtio_rx += self.echo_received_now.eq(echo_received_now)

        rx_fsm.act("INPUT",
            If(rx_dp.frame_r,
                rx_dp.packet_buffer_load.eq(1),
                If(rx_dp.packet_last,
                    Case(rx_dp.packet_type, {
                        rx_plm.types["error"]: NextState("ERROR"),
                        rx_plm.types["echo_reply"]: echo_received_now.eq(1),
                        rx_plm.types["fifo_space_reply"]: NextState("FIFO_SPACE"),
                        "default": [
                            error_not.eq(1),
                            error_code.eq(error_codes["unknown_type"])
                        ]
                    })
                )
            )
        )
        rx_fsm.act("ERROR",
            error_not.eq(1),
            error_code.eq(rx_dp.packet_as["error"].code),
            NextState("INPUT")
        )
        rx_fsm.act("FIFO_SPACE",
            fifo_space_not.eq(1),
            fifo_space.eq(rx_dp.packet_as["fifo_space_reply"].space),
            NextState("INPUT")
        )