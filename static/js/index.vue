<template id="page-lnurlmint">
  <div class="row q-col-gutter-md">
    <div class="col-12">
      <q-card>
        <q-card-section>
          <div class="row items-center q-mb-md">
            <div class="col">
              <h5 class="text-subtitle1 q-my-none">lnurlmint</h5>
            </div>
            <div class="col-auto">
              <q-btn
                unelevated
                color="primary"
                label="Create Mint"
                @click="openCreateDialog"
              ></q-btn>
            </div>
          </div>

          <q-banner v-if="errorMessage" class="bg-negative text-white q-mb-md">
            {{ errorMessage }}
            <template v-slot:action>
              <q-btn flat label="Dismiss" @click="errorMessage = ''" />
            </template>
          </q-banner>

          <q-linear-progress v-if="loading" color="primary" class="q-mb-md" />

          <q-list v-if="mints.length > 0" bordered separator>
            <q-expansion-item
              v-for="mint in mints"
              :key="mint.id"
              expand-separator
            >
              <template v-slot:header>
                <q-item-section>
                  <q-item-label>{{ mint.username }}</q-item-label>
                  <q-item-label caption>{{ mint.id }}</q-item-label>
                </q-item-section>
                <q-item-section side>
                  <div class="row q-gutter-xs">
                    <q-badge v-if="mint.sunset_mint" color="orange" label="Sunset" />
                    <q-badge
                      v-if="mint.verify_enabled"
                      color="blue"
                      label="Verify"
                    />
                  </div>
                </q-item-section>
                <q-item-section side>
                  <div class="row q-gutter-xs">
                    <q-btn
                      flat
                      dense
                      color="primary"
                      icon="edit"
                      @click.stop="openEditDialog(mint)"
                    ></q-btn>
                    <q-btn
                      flat
                      dense
                      color="negative"
                      icon="delete"
                      @click.stop="deleteMint(mint.id)"
                    ></q-btn>
                  </div>
                </q-item-section>
              </template>

              <q-card flat>
                <q-card-section>
                  <div class="row q-gutter-md">
                    <div class="col-6">
                      <div class="text-subtitle2 q-mb-sm">Outstanding Notes</div>
                      <q-linear-progress
                        v-if="notesLoading"
                        color="primary"
                        class="q-mb-sm"
                      />
                      <q-table
                        v-if="notes.length > 0"
                        dense
                        flat
                        :rows="notes"
                        :columns="noteColumns"
                        row-key="id"
                        :pagination="{ rowsPerPage: 5 }"
                      >
                        <template v-slot:body-cell-id="props">
                          <q-td :props="props">{{ truncateId(props.row.id) }}</q-td>
                        </template>
                        <template v-slot:body-cell-amount_msat="props">
                          <q-td :props="props">{{ formatSats(props.row.amount_msat) }}</q-td>
                        </template>
                        <template v-slot:body-cell-state="props">
                          <q-td :props="props">
                            <q-badge
                              :color="noteStateColor(props.row)"
                              :label="noteState(props.row)"
                            />
                          </q-td>
                        </template>
                      </q-table>
                      <div v-else-if="!notesLoading" class="text-grey text-caption">
                        No notes.
                      </div>
                      <q-btn
                        flat
                        dense
                        label="Refresh"
                        @click="fetchNotes(mint.id)"
                        class="q-mt-sm"
                      ></q-btn>
                    </div>
                    <div class="col-6">
                      <div class="text-subtitle2 q-mb-sm">Recent Activity</div>
                      <q-linear-progress
                        v-if="activityLoading"
                        color="primary"
                        class="q-mb-sm"
                      />
                      <q-table
                        v-if="activity.length > 0"
                        dense
                        flat
                        :rows="activity"
                        :columns="activityColumns"
                        row-key="payment_hash"
                        :pagination="{ rowsPerPage: 5 }"
                      >
                        <template v-slot:body-cell-type="props">
                          <q-td :props="props">
                            <q-badge
                              :color="props.row.type === 'mint' ? 'green' : 'purple'"
                              :label="props.row.type"
                            />
                          </q-td>
                        </template>
                        <template v-slot:body-cell-amount_msat="props">
                          <q-td :props="props">{{ formatSats(props.row.amount_msat) }}</q-td>
                        </template>
                        <template v-slot:body-cell-payment_hash="props">
                          <q-td :props="props">{{ truncateId(props.row.payment_hash) }}</q-td>
                        </template>
                        <template v-slot:body-cell-settled="props">
                          <q-td :props="props">
                            <q-badge
                              :color="props.row.settled ? 'green' : 'grey'"
                              :label="props.row.settled ? 'settled' : 'pending'"
                            />
                          </q-td>
                        </template>
                      </q-table>
                      <div v-else-if="!activityLoading" class="text-grey text-caption">
                        No activity yet.
                      </div>
                      <q-btn
                        flat
                        dense
                        label="Refresh"
                        @click="fetchActivity(mint.id)"
                        class="q-mt-sm"
                      ></q-btn>
                    </div>
                  </div>
                </q-card-section>
              </q-card>
            </q-expansion-item>
          </q-list>
          <div v-else-if="!loading" class="text-center text-grey q-pa-md">
            No mints yet. Click "Create Mint" to get started.
          </div>
        </q-card-section>
      </q-card>
    </div>

    <!-- Create Dialog -->
    <q-dialog v-model="createDialog.show">
      <q-card style="min-width: 400px">
        <q-card-section>
          <div class="text-h6">Create Mint</div>
        </q-card-section>
        <q-card-section>
          <q-form @submit.prevent="createMint">
            <q-input
              filled
              v-model="createDialog.data.username"
              label="Username"
              hint="A name for this mint"
              class="q-mb-md"
            ></q-input>
            <q-input
              filled
              type="number"
              v-model.number="createDialog.data.base_fee_msat"
              label="Base Fee (msat)"
              class="q-mb-md"
            ></q-input>
            <q-input
              filled
              type="number"
              v-model.number="createDialog.data.fee_percent_ppm"
              label="Fee Percent (ppm)"
              hint="Parts per million (10000 = 1%)"
              class="q-mb-md"
            ></q-input>
            <q-input
              filled
              type="number"
              v-model.number="createDialog.data.min_sendable_msat"
              label="Min Sendable (msat)"
              class="q-mb-md"
            ></q-input>
            <q-input
              filled
              type="number"
              v-model.number="createDialog.data.max_sendable_msat"
              label="Max Sendable (msat)"
              class="q-mb-md"
            ></q-input>
            <q-input
              filled
              type="number"
              v-model.number="createDialog.data.min_mint_msat"
              label="Min Mint (msat)"
              class="q-mb-md"
            ></q-input>
            <q-input
              filled
              v-model="createDialog.data.base_url"
              label="Base URL"
              hint="Clearnet public URL (optional)"
              class="q-mb-md"
            ></q-input>
            <q-input
              filled
              v-model="createDialog.data.onion_url"
              label="Onion URL"
              hint="Tor onion address (optional)"
              class="q-mb-md"
            ></q-input>
            <div class="row items-center q-mb-md">
              <q-toggle
                v-model="createDialog.data.verify_enabled"
                label="Verify Enabled"
              />
              <q-toggle
                v-model="createDialog.data.sunset_mint"
                label="Sunset Mode"
                class="q-ml-md"
              />
            </div>
            <div class="row justify-end">
              <q-btn
                flat
                label="Cancel"
                v-close-popup
                class="q-mr-sm"
              ></q-btn>
              <q-btn
                unelevated
                color="primary"
                label="Create"
                type="submit"
                :loading="createDialog.loading"
              ></q-btn>
            </div>
          </q-form>
        </q-card-section>
      </q-card>
    </q-dialog>

    <!-- Edit Dialog -->
    <q-dialog v-model="editDialog.show">
      <q-card style="min-width: 400px">
        <q-card-section>
          <div class="text-h6">Edit Mint</div>
        </q-card-section>
        <q-card-section>
          <q-form @submit.prevent="updateMint">
            <q-input
              filled
              v-model="editDialog.data.username"
              label="Username"
              class="q-mb-md"
            ></q-input>
            <q-input
              filled
              type="number"
              v-model.number="editDialog.data.base_fee_msat"
              label="Base Fee (msat)"
              class="q-mb-md"
            ></q-input>
            <q-input
              filled
              type="number"
              v-model.number="editDialog.data.fee_percent_ppm"
              label="Fee Percent (ppm)"
              class="q-mb-md"
            ></q-input>
            <q-input
              filled
              type="number"
              v-model.number="editDialog.data.min_sendable_msat"
              label="Min Sendable (msat)"
              class="q-mb-md"
            ></q-input>
            <q-input
              filled
              type="number"
              v-model.number="editDialog.data.max_sendable_msat"
              label="Max Sendable (msat)"
              class="q-mb-md"
            ></q-input>
            <q-input
              filled
              type="number"
              v-model.number="editDialog.data.min_mint_msat"
              label="Min Mint (msat)"
              class="q-mb-md"
            ></q-input>
            <q-input
              filled
              v-model="editDialog.data.base_url"
              label="Base URL"
              class="q-mb-md"
            ></q-input>
            <q-input
              filled
              v-model="editDialog.data.onion_url"
              label="Onion URL"
              class="q-mb-md"
            ></q-input>
            <div class="row items-center q-mb-md">
              <q-toggle
                v-model="editDialog.data.verify_enabled"
                label="Verify Enabled"
              />
              <q-toggle
                v-model="editDialog.data.sunset_mint"
                label="Sunset Mode"
                class="q-ml-md"
              />
            </div>
            <div class="row justify-end">
              <q-btn
                flat
                label="Cancel"
                v-close-popup
                class="q-mr-sm"
              ></q-btn>
              <q-btn
                unelevated
                color="primary"
                label="Save"
                type="submit"
                :loading="editDialog.loading"
              ></q-btn>
            </div>
          </q-form>
        </q-card-section>
      </q-card>
    </q-dialog>
  </div>
</template>
