use core::slice;
use board::{csr, mem};
use smoltcp::Error;
use smoltcp::phy::Device;

const RX0_BASE: usize = mem::ETHMAC_BASE + 0x0000;
const RX1_BASE: usize = mem::ETHMAC_BASE + 0x0800;
const TX0_BASE: usize = mem::ETHMAC_BASE + 0x1000;
const TX1_BASE: usize = mem::ETHMAC_BASE + 0x1800;

const RX_BUFFERS: [*mut u8; 2] = [RX0_BASE as *mut u8, RX1_BASE as *mut u8];
const TX_BUFFERS: [*mut u8; 2] = [TX0_BASE as *mut u8, TX1_BASE as *mut u8];

pub struct EthernetDevice;

impl Device for EthernetDevice {
    type RxBuffer = &'static [u8];
    type TxBuffer = TxBuffer;

    fn mtu(&self) -> usize { 1500 }

    fn receive(&mut self) -> Result<Self::RxBuffer, Error> {
        unsafe {
            if csr::ethmac::sram_writer_ev_pending_read() != 0 {
                let slot   = csr::ethmac::sram_writer_slot_read();
                let length = csr::ethmac::sram_writer_length_read();
                csr::ethmac::sram_writer_ev_pending_write(1);
                Ok(slice::from_raw_parts(RX_BUFFERS[slot as usize],
                                         length as usize))
            } else {
                Err(Error::Exhausted)
            }
        }
    }

    fn transmit(&mut self, length: usize) -> Result<Self::TxBuffer, Error> {
        unsafe {
            if csr::ethmac::sram_reader_ready_read() != 0 {
                let slot  = csr::ethmac::sram_reader_slot_read();
                let slot  = (slot + 1) % (TX_BUFFERS.len() as u8);
                csr::ethmac::sram_reader_slot_write(slot);
                csr::ethmac::sram_reader_length_write(length as u16);
                Ok(TxBuffer(slice::from_raw_parts_mut(TX_BUFFERS[slot as usize],
                                                      length as usize)))
            } else {
                Err(Error::Exhausted)
            }
        }
    }
}

pub struct TxBuffer(&'static mut [u8]);

impl AsRef<[u8]> for TxBuffer {
    fn as_ref(&self) -> &[u8] { self.0 }
}

impl AsMut<[u8]> for TxBuffer {
    fn as_mut(&mut self) -> &mut [u8] { self.0 }
}

impl Drop for TxBuffer {
    fn drop(&mut self) {
        unsafe { csr::ethmac::sram_reader_start_write(1) }
    }
}
